from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "security-camera-network",
    }
