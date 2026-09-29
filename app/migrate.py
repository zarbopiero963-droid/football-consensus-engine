from app.db import migrate

if __name__ == "__main__":
    migrate()
    print("schema applied")
