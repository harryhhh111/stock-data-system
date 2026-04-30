"""启动入口：python -m web 或 python -m uvicorn web.app:app --reload"""
import uvicorn


def main():
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
