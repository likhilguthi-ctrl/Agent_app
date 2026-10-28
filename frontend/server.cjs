const http = require("node:http");
const next = require("next");

const dev = process.env.NODE_ENV !== "production";
if (!process.env.NODE_ENV) {
  process.env.NODE_ENV = "development";
}
const args = process.argv.slice(2);

function optionValue(longName, shortName) {
  const index = args.findIndex(
    (argument) =>
      argument === longName ||
      argument === shortName ||
      argument.startsWith(`${longName}=`)
  );
  if (index === -1) return undefined;
  const argument = args[index];
  if (argument.startsWith(`${longName}=`)) {
    return argument.slice(longName.length + 1);
  }
  return args[index + 1];
}

const hostname =
  optionValue("--hostname", "-H") || process.env.HOST || "0.0.0.0";
const configuredPort =
  optionValue("--port", "-p") || process.env.PORT || "3000";
const port = Number(configuredPort);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("The frontend port must be an integer from 1 to 65535.");
}
const callbackPath = "/backend-api/api/v1/platform/auth/oidc/callback";
const callbackQueryHeader = "x-internal-platform-oidc-callback-query";
const app = next({ dev, hostname, port });
const handle = app.getRequestHandler();

app
  .prepare()
  .then(() => {
    const server = http.createServer((request, response) => {
      try {
        const baseHost =
          hostname.includes(":") && !hostname.startsWith("[")
            ? `[${hostname}]`
            : hostname;
        const requestUrl = new URL(request.url || "/", `http://${baseHost}`);
        const isOidcCallback =
          requestUrl.pathname === callbackPath ||
          requestUrl.pathname === `${callbackPath}/`;
        delete request.headers[callbackQueryHeader];
        if (isOidcCallback && requestUrl.search.length > 4096) {
          response.writeHead(400, {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
          });
          response.end('{"error":"INVALID_CALLBACK_QUERY"}');
          return;
        }
        if (isOidcCallback) {
          request.headers[callbackQueryHeader] = requestUrl.search;
          request.url = requestUrl.pathname;
        }
        void handle(request, response);
      } catch {
        response.writeHead(400, {
          "Cache-Control": "no-store",
          "Content-Type": "application/json",
        });
        response.end('{"error":"INVALID_REQUEST"}');
      }
    });

    server.listen(port, hostname, () => {
      process.stdout.write(`> Ready on http://${hostname}:${port}\n`);
    });
  })
  .catch(() => {
    process.stderr.write("Next.js server startup failed.\n");
    process.exitCode = 1;
  });
