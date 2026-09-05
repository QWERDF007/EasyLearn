from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send


class AssetResponse(FileResponse):
    """Delegate streaming to Starlette and route download failures to HTTP handlers."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_checked(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] >= 400:
                headers = Headers(raw=message["headers"])
                error_headers = {}
                if content_range := headers.get("content-range"):
                    error_headers["Content-Range"] = (
                        "bytes " + content_range
                        if content_range.startswith("*/")
                        else content_range
                    )
                raise HTTPException(message["status"], headers=error_headers)
            await send(message)

        await super().__call__(scope, receive, send_checked)
