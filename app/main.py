from app.api import app
from app.config import settings


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings["PORT"], reload=False)
