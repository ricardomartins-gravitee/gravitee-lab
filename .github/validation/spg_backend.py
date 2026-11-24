from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("X-SPG-Proof-Request") != "yes":
            self.send_error(422, "Shared request policy did not run")
            return
        body = b"shared request policy ran"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

ThreadingHTTPServer(("0.0.0.0", 8080), Fixture).serve_forever()
