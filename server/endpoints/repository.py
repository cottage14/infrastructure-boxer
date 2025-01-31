#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import plugins.basetypes
import plugins.session
import plugins.ldap
import re
import os
import aiohttp.client
import asyncio
import shutil
import asfpy.messaging

GIT_EXEC = shutil.which("git")
GB_CLONE_EXEC = "/x1/gitbox/bin/gitbox-clone"
NEW_REPO_NOTIFY = 'private@infra.apache.org'
NEW_REPO_NOTIFY_MSG = """
A new repository has been set up by %(uid)s@apache.org: %(reponame)s

Commit mail target: %(commit_mail)s
Dev/issue mail target: %(issue_mail)s

The repository can be found at:
GitBox: %(repourl_gb)s
GitHub: %(repourl_gh)s

With regards,
Boxer Git Management Services
"""

""" Repository editor endpoint for Boxer"""

GB_GITWEB_PATH = "/x1/gitbox/conf/httpd/gitweb.%(pmc)s.pl"
GB_GITWEB_CONFIG = """
our $projectroot = "/x1/repos/private/%(pmc)s";
our $site_name = "Private repositories for Apache%(pmc)s";
our $site_header = "<h1>Apache %(pmc)s Private Git Repos</h1>";

# Fix URLs for static assests to simplify the
# httpd configuration.
our @stylesheets = ("/static/gitweb.css");
our $logo = "/static/git-logo.png";
our $favicon = "/static/git-favicon.png";
our $javascript = "/static/gitweb.js";
$feature{'avatar'}{'default'} = ['gravatar'];
$feature{'highlight'}{'default'} = [1];

"""
EXEC_ADDITIONAL_PROJECTS = ["board", "members", "foundation"]

async def process(
        server: plugins.basetypes.Server, session: plugins.session.SessionObject, indata: dict
) -> dict:
    if not session.credentials:
        return {"okay": False, "message": "You need to be logged in to access this end point"}

    action = indata.get("action")
    if action == "create":
        reponame = indata.get("repository")
        uid = session.credentials.uid
        private = indata.get("private", False)
        m = re.match(r"^(?:incubator-)?([a-z0-9]+)(-[-0-9a-z]+)?\.git$", reponame)  # httpd.git or sling-foo.git etc
        if not m:
            return {"okay": False, "message": "Invalid repository name specified"}
        pmc = m.group(1)
        title = indata.get("title", "Apache %s" % pmc)

        # Check LDAP ownership
        if not session.credentials.admin and not (session.credentials.member and pmc in EXEC_ADDITIONAL_PROJECTS):
            async with plugins.ldap.LDAPClient(server.config.ldap) as lc:
                committer_list, pmc_list = await lc.get_members(pmc)
                if not pmc_list:
                    return {"okay": False, "message": "Invalid project prefix '%s' specified" % pmc}
                if session.credentials.uid not in pmc_list:
                    return {"okay": False, "message": "Only (I)PMC members of this project may create repositories"}

        repourl_gh = f"https://github.com/{server.config.github.org}/{reponame}"
        repourl_gb = f"https://gitbox.apache.org/repos/asf/{reponame}"
        if not private:
            repo_path = os.path.join(server.config.repos.public, reponame)
            if os.path.exists(repo_path):
                return {"okay": False, "message": "A repository by that name already exists"}
        else:
            if not session.credentials.admin:
                return {"okay": False, "message": "Private repositories can only be created by Infrastructure staff"}
            repourl_gb = f"https://gitbox.apache.org/repos/private/{pmc}/{reponame}"
            repo_path = os.path.join(server.config.repos.private, pmc, reponame)
            pmc_dir = os.path.join(server.config.repos.private, pmc)
            # If PMC dir does not exist, create it and plop in a .htaccess file for auth
            if not os.path.isdir(pmc_dir):
                os.mkdir(pmc_dir)
                htaccess = f"""
<Location /repos/private/{pmc}>
AuthType Basic
AuthName "ASF Private Repos for Apache {pmc}"
AuthBasicProvider ldap
AuthLDAPUrl "ldaps://ldap-eu-ro.apache.org/ou=people,dc=apache,dc=org?uid"
AuthLDAPGroupAttribute owner
AuthLDAPGroupAttributeIsDN on
Require ldap-group cn={pmc},ou=project,ou=groups,dc=apache,dc=org
</Location>
"""
                gitwebconf = f"""
our $projectroot = "{pmc_dir}";
our $site_name = "Private repositories for Apache {pmc}";
our $site_header = "<h1>ASF Private Git Repositories for Apache {pmc}</h1>";
our @stylesheets = ("/static/gitweb.css");
our $logo = "/static/git-logo.png";
our $favicon = "/static/git-favicon.png";
our $javascript = "/static/gitweb.js";
"""
                with open(f"/x1/gitbox/conf/httpd/gitweb.{pmc}.pl", "w") as f:
                    f.write(gitwebconf)
                    f.close()
                with open(f"/x1/gitbox/conf/httpd/htaccess.{pmc}", "w") as f:
                    f.write(htaccess)
                    f.close()

                proc = await asyncio.create_subprocess_exec(
                        '/usr/bin/sudo', '/usr/sbin/service', 'apache2', 'graceful',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return {"okay": False, "message": "Could not apply pre-create security controls: " + stderr.encode("utf-8")}

            if os.path.exists(repo_path):
                return {"okay": False, "message": "A repository by that name already exists"}

        # Get last bits of info
        commit_mail = indata.get("commit", "commits@%s.apache.org" % pmc)
        issue_mail = indata.get("issue", "dev@%s.apache.org" % pmc)

        # Create the repo
        if private and GB_GITWEB_PATH:
            with open(GB_GITWEB_PATH % locals(), "w") as f:
                f.write(GB_GITWEB_CONFIG % locals())
                f.close()
        rv = await create_repo(server, reponame, title, pmc, private)
        if rv is True:
            params = ['-c', commit_mail, '-d', title, "git@github:%s/%s" % (server.config.github.org, reponame),
                      repo_path]
            proc = await asyncio.create_subprocess_exec(
                GB_CLONE_EXEC, *params, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            # Everything went okay?
            if proc.returncode == 0:
                # Add the apache.dev setting
                with open(os.path.join(repo_path, "config"), "a") as f:
                    f.write("\n[apache]\n    dev = %s\n" % issue_mail)
                    f.close()
                asfpy.messaging.mail(
                    sender="GitBox <gitbox@apache.org>",
                    recipients=[NEW_REPO_NOTIFY, f"private@{pmc}.apache.org"],
                    subject=f"New GitBox/GitHub repository set up: {reponame}",
                    message=NEW_REPO_NOTIFY_MSG % locals()
                )
                return {"okay": True, "message": "Repository created!"}
            else:
                return {"okay": False, "message": str(stderr)}
        else:
            return {"okay": False, "message": rv}


async def create_repo(server, repo, title, pmc, private = False):
    url = "https://api.github.com/orgs/%s/repos" % server.config.github.org
    session_timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=15)
    async with aiohttp.client.ClientSession(timeout=session_timeout) as hc:
        rv = await hc.post(url, json={
                'name': repo,
                'description': title,
                'homepage': "https://%s.apache.org/" % pmc,
                'private': private,
                'has_issues': False,
                'has_projects': False,
                'has_wiki': False
            },
            headers={'Authorization': "token %s" % server.config.github.token}
        )
        if rv.status == 201:
            return True
        else:
            txt = await rv.text()
            return txt


def register(server: plugins.basetypes.Server):
    return plugins.basetypes.Endpoint(process)
