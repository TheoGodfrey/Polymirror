import sys
import os

print(f"Python Executable: {sys.executable}")
print(f"Search Paths: {sys.path}")

try:
    import py_clob_client
    print("✅ Success: Module found at", os.path.dirname(py_clob_client.__file__))
except ImportError as e:
    print(f"❌ Error: {e}")