import subprocess
import os
import sys
import tempfile
import base64

def main():
    if len(sys.argv) < 2:
        print("Usage: python convert.py <text_file>")
        sys.exit(1)

    input_file = sys.argv[1]

    # Read base64 content
    with open(input_file, "r") as f:
        b64_content = f.read().strip()

    # Decode to python code
    try:
        py_code = base64.b64decode(b64_content).decode("utf-8")
    except Exception as e:
        print(f"❌ Failed to read: {e}")
        sys.exit(1)

    # Create a temporary .py file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".py", text=True)
    os.close(temp_fd)  # Close low-level fd, we'll use high-level open()
    with open(temp_path, "w") as f:
        f.write(py_code)

    try:
        # Run the generated .py file
        subprocess.run([sys.executable, temp_path], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Error while running {temp_path}: {e}")
    finally:
        # Delete the temporary file
        os.remove(temp_path)

if __name__ == "__main__":
    main()