from dotenv import load_dotenv

# load_dotenv MUST run before importing server, because server.py
# triggers Config.from_env() at import time
load_dotenv()

from .server import mcp  # noqa: E402


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
