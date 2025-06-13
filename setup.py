#!/usr/bin/env python3
"""
Setup script for JediMaster - helps users get started quickly.
"""

import os
import sys
import subprocess
import shutil

def check_python_version():
    """Check if Python version is compatible."""
    print("Checking Python version...")
    if sys.version_info < (3, 8):
        print("❌ Python 3.8 or higher is required")
        return False
    print(f"✅ Python {sys.version.split()[0]} detected")
    return True

def check_pip():
    """Check if pip is available."""
    print("Checking pip...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"], 
                      check=True, capture_output=True)
        print("✅ pip is available")
        return True
    except subprocess.CalledProcessError:
        print("❌ pip is not available")
        return False

def install_dependencies():
    """Install required dependencies."""
    print("Installing dependencies...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], 
                      check=True)
        print("✅ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install dependencies: {e}")
        return False

def setup_environment():
    """Set up environment file."""
    print("Setting up environment file...")
    if os.path.exists('.env'):
        print("⚠️  .env file already exists, skipping creation")
        return True
    
    if os.path.exists('.env.example'):
        shutil.copy('.env.example', '.env')
        print("✅ Created .env file from template")
        print("📝 Please edit .env and add your API keys")
        return True
    else:
        print("❌ .env.example file not found")
        return False

def check_api_keys():
    """Check if API keys are configured."""
    print("Checking API key configuration...")
    
    # Load .env if it exists
    from dotenv import load_dotenv
    load_dotenv()
    
    # Check environment variables (from .env or system)
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    github_configured = github_token is not None and github_token != 'your_github_token_here'
    openai_configured = openai_api_key is not None and openai_api_key != 'your_openai_api_key_here'
    
    if github_configured and openai_configured:
        print("✅ API keys are configured")
        # Check source of configuration
        env_file_exists = os.path.exists('.env')
        if env_file_exists:
            with open('.env', 'r') as f:
                env_content = f.read()
            github_in_file = 'GITHUB_TOKEN=' in env_content and 'your_github_token_here' not in env_content
            openai_in_file = 'OPENAI_API_KEY=' in env_content and 'your_openai_api_key_here' not in env_content
            
            if github_in_file or openai_in_file:
                print("   (Found in .env file)")
            else:
                print("   (Found in system environment variables)")
        else:
            print("   (Found in system environment variables)")
        return True
    else:
        print("⚠️  API keys need to be configured")
        if not github_configured:
            print("   - GITHUB_TOKEN not configured")
        if not openai_configured:
            print("   - OPENAI_API_KEY not configured")
        print("   Configure them in .env file or as system environment variables")
        return False

def run_test():
    """Run the test script to validate setup."""
    print("Running validation tests...")
    try:
        result = subprocess.run([sys.executable, "test_setup.py"], capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print("Warnings/Errors:")
            print(result.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"❌ Failed to run tests: {e}")
        return False

def main():
    """Run the complete setup process."""
    print("🚀 JediMaster Setup Script")
    print("=" * 40)
    
    steps = [
        ("Python Version", check_python_version),
        ("Pip Availability", check_pip),
        ("Install Dependencies", install_dependencies),
        ("Environment Setup", setup_environment),
    ]
    
    for step_name, step_func in steps:
        print(f"\n📋 {step_name}")
        if not step_func():
            print(f"\n❌ Setup failed at: {step_name}")
            return 1
    
    print(f"\n📋 API Key Configuration")
    api_keys_ok = check_api_keys()
    
    if api_keys_ok:
        print(f"\n📋 Validation Tests")
        if run_test():
            print("\n🎉 Setup completed successfully!")
            print("\nYou can now use JediMaster:")
            print("  python jedimaster.py owner/repo")
        else:
            print("\n⚠️  Setup completed but some tests failed")
            print("Check the test output above for details")
    else:
        print(f"\n⚠️  Setup completed but API keys need configuration")
        print("\nNext steps:")
        print("1. Edit .env file and add your API keys:")
        print("   - GITHUB_TOKEN: Get from https://github.com/settings/tokens")
        print("   - OPENAI_API_KEY: Get from https://platform.openai.com/api-keys")
        print("2. Run: python test_setup.py")
        print("3. Use: python jedimaster.py owner/repo")
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
