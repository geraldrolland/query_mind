"""Test running alembic migration upgrade."""
import sys
import os

# Add backend to path
backend_dir = r"C:\Users\USER\INSIGTHLY_PROJECT\query_mind\backend"
sys.path.insert(0, backend_dir)

# Change to backend directory
old_cwd = os.getcwd()
os.chdir(backend_dir)

try:
    from alembic.config import Config
    config = Config("alembic.ini")
    print(f"Script location: {config.get_main_option('script_location')}")
    print(f"SQL URL: {config.get_main_option('sqlalchemy.url')}")
    
    from alembic import command
    command.upgrade(config, "head")
    print("\n✓ SUCCESS: Migration applied successfully!")
    print("✓ The 'message.status' column should now be in the database.")
except Exception as e:
    print(f"\n✗ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
finally:
    os.chdir(old_cwd)
    print("\nDatabase migration test completed.")