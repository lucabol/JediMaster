"""
OpenAI client utilities for JediMaster.
"""

import os
from openai import OpenAI


def create_openai_client(api_key: str, model: str) -> OpenAI:
    """
    Create an OpenAI client configured for either Azure OpenAI or standard OpenAI.
    
    Args:
        api_key: The OpenAI API key
        model: The model name (used for Azure OpenAI deployment path)
    
    Returns:
        Configured OpenAI client
    """
    base_url = os.getenv('OPENAI_BASE_URL')
    client_kwargs = {"api_key": api_key}
    
    if base_url and base_url.strip():
        # Azure OpenAI configuration
        client_kwargs["base_url"] = f"{base_url}/openai/deployments/{model}"
        client_kwargs["default_query"] = {"api-version": "2024-02-15-preview"}
    else:
        # Standard OpenAI configuration
        client_kwargs["base_url"] = "https://api.openai.com/v1"
    
    return OpenAI(**client_kwargs)