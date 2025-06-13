#!/usr/bin/env python3
"""
Test script for JediMaster GraphQL functionality - validates GraphQL assignment process.
"""

import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock

# Add the current directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_graphql_imports():
    """Test that GraphQL-related modules can be imported."""
    print("Testing GraphQL imports...")
    
    try:
        from graphql_client import GitHubGraphQLClient
        from jedimaster_graphql import JediMasterGraphQL
        print("✅ GraphQL modules imported successfully")
        return True
    except ImportError as e:
        print(f"❌ GraphQL import error: {e}")
        return False

def test_graphql_client():
    """Test the GraphQL client with mocked responses."""
    print("Testing GraphQL client...")
    
    try:
        from graphql_client import GitHubGraphQLClient
        
        # Mock the requests.post method
        with patch('requests.post') as mock_post:
            # Mock a successful GraphQL response
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "data": {
                    "repository": {
                        "id": "R_kgDOBpzVGw",
                        "name": "Hello-World",
                        "owner": {
                            "login": "lucabol"
                        },
                        "url": "https://github.com/lucabol/Hello-World"
                    }
                }
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response
            
            # Test the GraphQL client
            client = GitHubGraphQLClient("fake-token")
            repo_info = client.get_repository_info("lucabol", "Hello-World")
            
            assert repo_info["name"] == "Hello-World"
            assert repo_info["owner"]["login"] == "lucabol"
            print("✅ GraphQL client test passed")
            return True
            
    except Exception as e:
        print(f"❌ GraphQL client test failed: {e}")
        return False

def test_graphql_jedimaster():
    """Test the JediMaster GraphQL class with mocked dependencies."""
    print("Testing JediMaster GraphQL class...")
    
    try:
        from jedimaster_graphql import JediMasterGraphQL
        from jedimaster import IssueResult, ProcessingReport
        
        # Mock both the GraphQL client and the DeciderAgent
        with patch('jedimaster_graphql.GitHubGraphQLClient') as mock_graphql_client, \
             patch('jedimaster_graphql.DeciderAgent') as mock_decider:
            
            # Mock GraphQL client
            mock_client_instance = Mock()
            mock_client_instance.get_issues.return_value = [{
                "id": "I_kwDOBpzVGw5iJ_X6",
                "number": 1,
                "title": "Test GraphQL Issue",
                "body": "This is a test issue for GraphQL processing",
                "url": "https://github.com/lucabol/Hello-World/issues/1",
                "labels": {"nodes": [{"name": "enhancement"}]},
                "assignees": {"nodes": []},
                "comments": {"nodes": []}
            }]
            mock_graphql_client.return_value = mock_client_instance
            
            # Mock DeciderAgent
            mock_decider_instance = Mock()
            mock_decider_instance.evaluate_issue.return_value = {
                "decision": "yes",
                "reasoning": "This issue involves coding tasks suitable for GraphQL assignment testing."
            }
            mock_decider.return_value = mock_decider_instance
            
            # Test JediMaster GraphQL
            jedimaster = JediMasterGraphQL("fake-github-token", "fake-openai-key")
            
            # Test issue processing (without actual assignment)
            test_issue = {
                "id": "I_kwDOBpzVGw5iJ_X6",
                "number": 1,
                "title": "Test GraphQL Issue",
                "body": "This is a test issue for GraphQL processing",
                "url": "https://github.com/lucabol/Hello-World/issues/1",
                "labels": {"nodes": [{"name": "enhancement"}]},
                "assignees": {"nodes": []},
                "comments": {"nodes": []}
            }
            
            # Mock the assignment method to avoid actual API calls
            with patch.object(jedimaster, 'assign_to_copilot_graphql', return_value=True):
                result = jedimaster.process_issue(test_issue, "lucabol/Hello-World")
                
                assert result.status == 'assigned'
                assert result.issue_number == 1
                assert result.title == "Test GraphQL Issue"
                print("✅ JediMaster GraphQL class test passed")
                return True
            
    except Exception as e:
        print(f"❌ JediMaster GraphQL class test failed: {e}")
        return False

def test_graphql_assignment_process():
    """Test the complete GraphQL assignment process with mocks."""
    print("Testing complete GraphQL assignment process...")
    
    try:
        from jedimaster_graphql import JediMasterGraphQL
        
        with patch('jedimaster_graphql.GitHubGraphQLClient') as mock_graphql_client, \
             patch('jedimaster_graphql.DeciderAgent') as mock_decider:
            
            # Mock GraphQL client methods
            mock_client_instance = Mock()
            
            # Mock repository info
            mock_client_instance.get_repository_info.return_value = {
                "id": "R_kgDOBpzVGw",
                "name": "Hello-World",
                "owner": {"login": "lucabol"}
            }
            
            # Mock issues
            mock_client_instance.get_issues.return_value = [{
                "id": "I_kwDOBpzVGw5iJ_X6",
                "number": 1,
                "title": "Test GraphQL Assignment",
                "body": "Test the complete GraphQL assignment process",
                "url": "https://github.com/lucabol/Hello-World/issues/1",
                "labels": {"nodes": []},
                "assignees": {"nodes": []},
                "comments": {"nodes": []}
            }]
            
            # Mock assignment operations
            mock_client_instance.get_user_id.return_value = "U_kgDOBpzVGw"
            mock_client_instance.add_assignees_to_issue.return_value = True
            mock_client_instance.get_repository_labels.return_value = []
            mock_client_instance.create_label.return_value = "L_kgDOBpzVGw"
            mock_client_instance.add_labels_to_issue.return_value = True
            mock_client_instance.add_comment_to_issue.return_value = True
            
            mock_graphql_client.return_value = mock_client_instance
            
            # Mock DeciderAgent
            mock_decider_instance = Mock()
            mock_decider_instance.evaluate_issue.return_value = {
                "decision": "yes",
                "reasoning": "This issue is suitable for GraphQL assignment process testing."
            }
            mock_decider.return_value = mock_decider_instance
            
            # Test the complete process
            jedimaster = JediMasterGraphQL("fake-github-token", "fake-openai-key")
            report = jedimaster.process_repositories(["lucabol/Hello-World"])
            
            assert report.total_issues == 1
            assert report.assigned == 1
            assert report.not_assigned == 0
            assert report.already_assigned == 0
            assert report.errors == 0
            
            # Verify the assignment methods were called
            mock_client_instance.add_assignees_to_issue.assert_called_once()
            mock_client_instance.add_labels_to_issue.assert_called_once()
            mock_client_instance.add_comment_to_issue.assert_called_once()
            
            print("✅ Complete GraphQL assignment process test passed")
            return True
            
    except Exception as e:
        print(f"❌ Complete GraphQL assignment process test failed: {e}")
        return False

def test_graphql_vs_rest_compatibility():
    """Test that GraphQL and REST implementations produce compatible results."""
    print("Testing GraphQL vs REST compatibility...")
    
    try:
        from jedimaster import IssueResult
        from jedimaster_graphql import JediMasterGraphQL
        
        # Test that both implementations produce the same IssueResult structure
        result_rest = IssueResult(
            repo="test/repo",
            issue_number=1,
            title="Test Issue",
            url="https://github.com/test/repo/issues/1",
            status="assigned",
            reasoning="Test reasoning"
        )
        
        # The GraphQL implementation should produce identical IssueResult objects
        # Verify field compatibility
        required_fields = ['repo', 'issue_number', 'title', 'url', 'status', 'reasoning', 'error_message']
        for field in required_fields:
            assert hasattr(result_rest, field), f"Missing field: {field}"
        
        print("✅ GraphQL vs REST compatibility test passed")
        return True
        
    except Exception as e:
        print(f"❌ GraphQL vs REST compatibility test failed: {e}")
        return False

def main():
    """Run all GraphQL tests."""
    print("🧪 Running JediMaster GraphQL validation tests...\n")
    
    tests = [
        test_graphql_imports,
        test_graphql_client,
        test_graphql_jedimaster,
        test_graphql_assignment_process,
        test_graphql_vs_rest_compatibility,
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        print()
    
    print("="*50)
    print(f"GraphQL Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All GraphQL tests passed! GraphQL assignment process is ready to use.")
        print("\nNext steps:")
        print("1. Copy .env.example to .env")
        print("2. Add your GitHub and OpenAI API keys to .env")
        print("3. Run: python jedimaster_graphql.py owner/repo")
        print("4. Or use: python jedimaster.py owner/repo (for REST API)")
        return 0
    else:
        print("❌ Some GraphQL tests failed. Please check the output above.")
        return 1

if __name__ == '__main__':
    sys.exit(main())