#!/usr/bin/env python3
"""Deploy a Structured Text program to the OpenPLC runtime through its web interface.

Logs in, uploads, compiles and (by default) restarts the runtime. Standard library only.

    python3 tools/deploy.py plc/control.st

Credentials come from OPENPLC_USER / OPENPLC_PASSWORD. The defaults are the container
image's defaults and only make sense for a runtime bound to localhost.
"""
import argparse
import http.cookiejar
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STATUS_RE = re.compile(r"Status: <font color = '[^']*'>(\w+)</font>")
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r'(\w+)\s*=\s*["\']([^"\']*)["\']')


class OpenplcWeb:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._post("/login", urllib.parse.urlencode(
            {"username": user, "password": password}).encode())
        if not re.search(r'id\s*=\s*"uploadForm"', self._get("/programs")):
            sys.exit("login failed: check OPENPLC_USER / OPENPLC_PASSWORD")

    def _get(self, path):
        with self.opener.open(self.url + path, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def _post(self, path, data, headers=None):
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        with self.opener.open(req, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")

    def _get_logs(self):
        """The log route answers 500 until the first compilation has run."""
        try:
            return self._get("/compilation-logs")
        except urllib.error.HTTPError:
            return None

    def status(self):
        match = STATUS_RE.search(self._get("/dashboard"))
        return match.group(1) if match else "unknown"

    def compile(self, path, name, timeout_s):
        with open(path, "rb") as handle:
            source = handle.read()
        boundary = "----openplc-deploy-%d" % time.time_ns()
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: text/plain\r\n\r\n' % (boundary, os.path.basename(path))).encode()
        body = head + source + ("\r\n--%s--\r\n" % boundary).encode()
        page = self._post("/upload-program", body,
                          {"Content-Type": "multipart/form-data; boundary=%s" % boundary})

        hidden = {}
        for tag in INPUT_RE.findall(page):
            attrs = dict(ATTR_RE.findall(tag))
            if attrs.get("type", "").lower() == "hidden" and "name" in attrs:
                hidden[attrs["name"]] = attrs.get("value", "")
        if "prog_file" not in hidden:
            error = re.search(r"<h2>Error</h2><p>(.*?)</p>", page, re.S)
            sys.exit("upload failed: %s" % (error.group(1) if error else "no prog_file in response"))

        fields = {"prog_name": name, "prog_descr": "deployed with tools/deploy.py"}
        fields.update(hidden)
        self._post("/upload-program-action", urllib.parse.urlencode(fields).encode())

        previous = self._get_logs()
        self._get("/compile-program?file=" + urllib.parse.quote(hidden["prog_file"]))
        deadline = time.time() + timeout_s
        # The compile runs on its own and the compiler rewrites the log from its first line, so a
        # new compile shows up as unfinished text even when the finished log will read the same
        # as the previous one (same program compiled twice).
        started = previous is None
        while True:
            time.sleep(0.5)
            log = self._get_logs()
            started = started or bool(log and log != previous)
            # the script prints "Compilation finished successfully!" or "... with errors!"
            if started and log and "Compilation finished" in log:
                return "finished successfully" in log, log
            if time.time() > deadline:
                return False, log or "(no compilation log was produced)"

    def restart(self):
        if self.status() == "Running":
            self._get("/stop_plc")
            time.sleep(1)
        self._get("/start_plc")
        time.sleep(2)
        return self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("program", help=".st file to deploy")
    parser.add_argument("--name", help="name shown in OpenPLC (default: file stem)")
    parser.add_argument("--url", default=os.environ.get("OPENPLC_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--user", default=os.environ.get("OPENPLC_USER", "openplc"))
    parser.add_argument("--password", default=os.environ.get("OPENPLC_PASSWORD", "openplc"))
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds to wait for the compiler (default: 180)")
    parser.add_argument("--no-start", action="store_true",
                        help="compile only, leave the runtime in its current state")
    args = parser.parse_args()

    web = OpenplcWeb(args.url, args.user, args.password)
    name = args.name or os.path.splitext(os.path.basename(args.program))[0]
    ok, log = web.compile(args.program, name, args.timeout)
    print(log.strip())
    if not ok:
        sys.exit("compilation failed")
    if args.no_start:
        print("compiled; runtime left %s" % web.status())
        return
    state = web.restart()
    print("runtime: %s" % state)
    if state != "Running":
        sys.exit(1)


if __name__ == "__main__":
    main()
