
"""
Product Catalog API - Application Entry Point

This file serves as the main entry point for the application.
It can be used with various WSGI/ASGI servers.
"""

from src.api.app import app

# For uvicorn/gunicorn
application = app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)