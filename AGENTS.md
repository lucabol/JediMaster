# Agent Instructions for GitHub Copilot

This document provides instructions for AI agents (including GitHub Copilot) working on this repository.

## Testing Requirements

### For All Pull Requests

When working on any issue or pull request, you **MUST**:

1. **Run All Existing Tests**
   - Execute the full test suite before submitting your PR
   - Ensure all tests pass
   - If any test fails, fix the issue before proceeding
   - Document test results in your PR description

2. **Add New Tests**
   - Write tests for any new functionality you add
   - Add tests that cover edge cases and error conditions
   - Ensure test coverage doesn't decrease
   - Follow the existing test patterns and conventions in the `tests/` directory

3. **Test Execution Commands**
   ```bash
   # Run all tests
   pytest
   
   # Run tests with coverage
   pytest --cov=. --cov-report=term-missing
   
   # Run specific test file
   pytest tests/test_<module>.py
   ```

### Test Coverage Guidelines

- **Minimum coverage**: Maintain or improve the current test coverage percentage
- **Critical paths**: Ensure 100% coverage for:
  - Authentication and credential handling
  - GitHub API interactions
  - Issue and PR processing logic
  - State transitions and workflow management
  - Agent decision-making logic

### Types of Tests to Include

1. **Unit Tests**
   - Test individual functions and methods in isolation
   - Mock external dependencies (GitHub API, Azure AI)
   - Fast execution (< 1 second per test)

2. **Integration Tests**
   - Test interactions between components
   - Test GitHub API integration
   - Test agent workflow end-to-end

3. **Edge Case Tests**
   - Empty inputs
   - Null/None values
   - Rate limiting scenarios
   - Network failures
   - Invalid responses from APIs

### Test Documentation

Include in your PR description:
- Which tests were run
- Test results (pass/fail counts)
- New tests added and what they cover
- Any tests that were modified and why
- Coverage changes (before/after percentages)

## Code Quality Requirements

### Before Submitting a PR

1. **Linting**
   - Code follows PEP 8 style guidelines
   - No linting errors or warnings
   - Run: `pylint *.py` or your configured linter

2. **Type Hints**
   - Add type hints to all function signatures
   - Helps with IDE support and catches errors early

3. **Documentation**
   - Add docstrings to new functions and classes
   - Update README.md if adding new features
   - Document any configuration changes

4. **Error Handling**
   - Add proper try/except blocks for external API calls
   - Log errors appropriately
   - Provide meaningful error messages

## Workflow-Specific Instructions

### Working on Issues

When assigned to an issue:

1. **Understand the requirement**
   - Read the issue description carefully
   - Check for any related issues or PRs
   - Ask questions if requirements are unclear

2. **Implement the solution**
   - Follow existing code patterns
   - Keep changes minimal and focused
   - Don't introduce unrelated changes

3. **Test thoroughly**
   - Write tests first (TDD approach recommended)
   - Test happy path and error cases
   - Test with realistic data

4. **Document your changes**
   - Update docstrings
   - Add comments for complex logic
   - Update relevant documentation files

### Pull Request Checklist

Before marking your PR as ready for review:

- [ ] All existing tests pass
- [ ] New tests added for new functionality
- [ ] Code coverage maintained or improved
- [ ] No linting errors
- [ ] Documentation updated
- [ ] Error handling added where needed
- [ ] Logging added for debugging
- [ ] Type hints included
- [ ] PR description includes test results
- [ ] Commit messages are clear and descriptive

## Testing Best Practices

### Writing Good Tests

```python
def test_function_name_should_expected_behavior():
    """Test that function_name returns expected result when given valid input."""
    # Arrange
    input_data = "test_value"
    expected_result = "expected_output"
    
    # Act
    actual_result = function_name(input_data)
    
    # Assert
    assert actual_result == expected_result
```

### Mocking External Dependencies

```python
from unittest.mock import Mock, patch

@patch('module.external_api_call')
def test_with_mocked_api(mock_api):
    """Test function that calls external API."""
    # Setup mock
    mock_api.return_value = {"status": "success"}
    
    # Test your function
    result = my_function()
    
    # Verify
    assert result is not None
    mock_api.assert_called_once()
```

### Testing Async Functions

```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    """Test async function."""
    result = await async_function()
    assert result == expected_value
```

## Common Pitfalls to Avoid

1. **Don't skip tests** - "It's a small change" is not an excuse
2. **Don't only test the happy path** - Test failures and edge cases
3. **Don't commit failing tests** - Fix or skip them with a TODO
4. **Don't reduce test coverage** - Always maintain or improve coverage
5. **Don't mock everything** - Some integration testing is valuable
6. **Don't write flaky tests** - Tests should be deterministic
7. **Don't test implementation details** - Test behavior, not internals

## Questions or Issues?

If you encounter problems with testing:
- Check existing test files for examples
- Review test documentation in `tests/README.md` (if available)
- Ask for clarification in the PR comments
- Check CI/CD logs for detailed error messages

## Continuous Improvement

As you work on this codebase:
- Suggest improvements to the testing infrastructure
- Add missing tests for existing code
- Improve test documentation
- Share testing best practices you discover

---

**Remember**: Good tests are as important as good code. They ensure reliability, catch regressions, and make future changes safer.
