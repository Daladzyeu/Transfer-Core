def main() -> None:
    import uvicorn

    uvicorn.run(
        "transfercore.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
    )
