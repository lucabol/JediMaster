#!/usr/bin/env python3
"""
Test script for JediMaster - validates basic functionality without making API calls.
"""

import sys
import os
import json
from unittest.mock import Mock, patch

# Add the current directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Test that all required modules can be imported."""
    print("Testing imports...")
    
    try:
        import requests
        import openai
        from dotenv import load_dotenv
        from github import Github
        print("✅ All required packages imported successfully")
        return True
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False

def test_decider_agent():
    """Test the DeciderAgent with mocked OpenAI responses."""
    print("Testing DeciderAgent...")
    
    try:
        from decider import DeciderAgent
        
        # Create a mock OpenAI client
        with patch('decider.OpenAI') as mock_openai:
            # Mock the completion response
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message.content = json.dumps({
                "decision": "yes",
                "reasoning": "This is a test issue that involves coding tasks."
            })
            
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_openai.return_value = mock_client
            
            # Test the decider
            decider = DeciderAgent("fake-api-key")
            
            test_issue = {
                "title": "Add error handling to API client",
                "body": "We need to add proper error handling to our API client code.",
                "labels": ["bug", "enhancement"],
                "comments": []
            }
            
            result = decider.evaluate_issue(test_issue)
            
            assert result["decision"] == "yes"
            assert "reasoning" in result
            print("✅ DeciderAgent test passed")
            return True
            
    except Exception as e:
        print(f"❌ DeciderAgent test failed: {e}")
        return False

def test_jedimaster_class():
    """Test the JediMaster class with mocked dependencies."""
    print("Testing JediMaster class...")
    
    try:
        from jedimaster import JediMaster, IssueResult, ProcessingReport
        
        # Test data structures
        result = IssueResult(
            repo="test/repo",
            issue_number=1,
            title="Test Issue",
            url="https://github.com/test/repo/issues/1",
            status="assigned",
            reasoning="Test reasoning"
        )
        
        report = ProcessingReport(
            total_issues=1,
            assigned=1,
            not_assigned=0,
            already_assigned=0,
            errors=0,
            results=[result],
            timestamp="2025-06-13T00:00:00"
        )
        
        print("✅ JediMaster class structure test passed")
        return True
        
    except Exception as e:
        print(f"❌ JediMaster class test failed: {e}")
        return False

def test_environment_example():
    """Test that the environment example file exists and is properly formatted."""
    print("Testing environment example file...")
    
    try:
        if not os.path.exists('.env.example'):
            print("❌ .env.example file not found")
            return False
        
        with open('.env.example', 'r') as f:
            content = f.read()
            
        required_vars = ['GITHUB_TOKEN', 'OPENAI_API_KEY']
        for var in required_vars:
            if var not in content:
                print(f"❌ Required environment variable {var} not found in .env.example")
                return False
        
        print("✅ Environment example file test passed")
        return True
        
    except Exception as e:
        print(f"❌ Environment example file test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("🧪 Running JediMaster validation tests...\n")
    
    tests = [
        test_imports,
        test_environment_example,
        test_decider_agent,
        test_jedimaster_class,
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        print()
    
    print("="*50)
    print(f"Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! JediMaster is ready to use.")
        print("\nNext steps:")
        print("1. Copy .env.example to .env")
        print("2. Add your GitHub and OpenAI API keys to .env")
        print("3. Run: python jedimaster.py owner/repo")
        return 0
    else:
        print("❌ Some tests failed. Please check the output above.")
        return 1

if __name__ == '__main__':
    sys.exit(main())
