#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""ASF Infrastructure Boxer - GitHub/GitBox accounts/teams suite"""
import argparse
import asyncio
import importlib
import json
import os
import sys
import traceback

import aiohttp.web
import yaml
import uuid

import plugins.background
import plugins.basetypes
import plugins.configuration
import plugins.database
import plugins.formdata
import plugins.session

BOXER_VERSION = "0.1.0"


class Server(plugins.basetypes.Server):
    """Main server class, responsible for handling requests and scheduling offloader threads """

    def __init__(self, args: argparse.Namespace):
        print(
            "==== ASF Infra Boxer Suite v/%s starting... ====" % BOXER_VERSION
        )
        # Load configuration
        yml = yaml.safe_load(open(args.config))
        self.config = plugins.configuration.Configuration(yml)
        self.data = plugins.configuration.InterData()
        self.handlers = dict()
        self.database = plugins.database.Database(self.config.database)
        self.server = None

        # Load each URL endpoint
        for endpoint_file in os.listdir("endpoints"):
            if endpoint_file.endswith(".py"):
                endpoint = endpoint_file[:-3]
                m = importlib.import_module(f"endpoints.{endpoint}")
                if hasattr(m, "register"):
                    self.handlers[endpoint] = m.__getattribute__("register")(self)
                    print(f"Registered endpoint /api/{endpoint}")
                else:
                    print(
                        f"Could not find entry point 'register()' in {endpoint_file}, skipping!"
                    )

    async def handle_request(
        self, request: aiohttp.web.BaseRequest
    ) -> aiohttp.web.Response:
        """Generic handler for all incoming HTTP requests"""
        resp: aiohttp.web.Response

        # Define response headers first...
        headers = {
            "Server": "ASF Infra Boxer Suite v/%s" % BOXER_VERSION,
        }

        # Figure out who is going to handle this request, if any
        # We are backwards compatible with the old Lua interface URLs
        body_type = "form"
        handler = request.path.split("/")[-1]
        if handler.endswith(".json"):
            body_type = "json"
            handler = handler[:-5]

        # Parse form data if any
        try:
            indata = await plugins.formdata.parse_formdata(body_type, request)
        except ValueError as e:
            return aiohttp.web.Response(headers=headers, status=400, text=str(e))

        # Find a handler, or 404
        if handler in self.handlers:
            session = await plugins.session.get_session(self, request)
            try:
                # Wait for endpoint response. This is typically JSON in case of success,
                # but could be an exception (that needs a traceback) OR
                # it could be a custom response, which we just pass along to the client.
                output = await self.handlers[handler].exec(self, session, indata)
                if session.database:
                    session.database = None
                headers["content-type"] = "application/json"
                if output and not isinstance(output, aiohttp.web.Response):
                    jsout = json.dumps(output, indent=2)
                    headers["Content-Length"] = str(len(jsout))
                    return aiohttp.web.Response(headers=headers, status=200, text=jsout)
                elif isinstance(output, aiohttp.web.Response):
                    return output
                else:
                    return aiohttp.web.Response(
                        headers=headers, status=404, text="Content not found"
                    )
            # If a handler hit an exception, we need to print that exception somewhere,
            # either to the web client or stderr:
            except:  # This is a broad exception on purpose!
                if session.database:
                    session.database = None
                exc_type, exc_value, exc_traceback = sys.exc_info()
                err = "\n".join(
                    traceback.format_exception(exc_type, exc_value, exc_traceback)
                )
                # By default, we print the traceback to the user, for easy debugging.
                if self.config.server.traceback:
                    return aiohttp.web.Response(
                        headers=headers, status=500, text="API error occurred: \n" + err
                    )
                # If client traceback is disabled, we print it to stderr instead, but leave an
                # error ID for the client to report back to the admin. Every line of the traceback
                # will have this error ID at the beginning of the line, for easy grepping.
                else:
                    # We only need a short ID here, let's pick 18 chars.
                    eid = str(uuid.uuid4())[:18]
                    sys.stderr.write("API Endpoint %s got into trouble (%s): \n" % (request.path, eid))
                    for line in err.split("\n"):
                        sys.stderr.write("%s: %s\n" % (eid, line))
                    return aiohttp.web.Response(
                        headers=headers, status=500, text="API error occurred. The application journal will have "
                                                          "information. Error ID: %s" % eid
                    )
        else:
            return aiohttp.web.Response(
                headers=headers, status=404, text="API Endpoint not found!"
            )

    async def server_loop(self, loop: asyncio.AbstractEventLoop):  # Note, loop never used.
        self.server = aiohttp.web.Server(self.handle_request)
        runner = aiohttp.web.ServerRunner(self.server)
        await runner.setup()
        site = aiohttp.web.TCPSite(
            runner, self.config.server.ip, self.config.server.port
        )
        await site.start()
        print(
            "==== Serving up Infra goodness at %s:%s ===="
            % (self.config.server.ip, self.config.server.port)
        )
        await plugins.background.run_tasks(self)

    def run(self):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self.server_loop(loop))
        except KeyboardInterrupt:
            pass
        loop.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        help="Configuration file to load (default: boxer.yaml)",
        default="boxer.yaml",
    )
    cliargs = parser.parse_args()
    pubsub_server = Server(cliargs)
    pubsub_server.run()
