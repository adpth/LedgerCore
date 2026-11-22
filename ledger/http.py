from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LimitRequestBody:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 16 * 1024:
                await JSONResponse({"detail": "Request body exceeds 16 KiB"}, 413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
