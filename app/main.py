from __future__ import annotations

import uvicorn

from app.api.routes import create_app

app = create_app()


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
