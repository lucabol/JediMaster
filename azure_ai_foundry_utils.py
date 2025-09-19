"""
Azure AI Foundry client utilities for JediMaster.
"""

import os
from typing import Optional
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider


def create_azure_ai_foundry_client(endpoint: str, api_key: str = None):
    """
    Create an Azure OpenAI client configured for Azure AI Foundry using managed authentication.
    
    Args:
        endpoint: The Azure AI Foundry endpoint (can be full OpenAI endpoint or base endpoint)
    
    Returns:
        Configured AzureOpenAI client using DefaultAzureCredential
    """
    # Parse the endpoint to extract components
    if "/openai/deployments/" in endpoint:
        # Extract base endpoint and other components from full OpenAI endpoint
        # Example: https://jedimaster-resource.cognitiveservices.azure.com/openai/deployments/model-router/chat/completions?api-version=2025-01-01-preview
        import urllib.parse
        parsed = urllib.parse.urlparse(endpoint)
        base_endpoint = f"{parsed.scheme}://{parsed.netloc}"
        
        # Extract API version from query parameters
        query_params = urllib.parse.parse_qs(parsed.query)
        api_version = query_params.get('api-version', ['2024-12-01-preview'])[0]
    else:
        # Already a base endpoint
        base_endpoint = endpoint
        api_version = "2024-12-01-preview"
    
    # Create token provider using DefaultAzureCredential
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), 
        "https://cognitiveservices.azure.com/.default"
    )
    
    # Create the Azure OpenAI client with managed identity authentication
    client = AzureOpenAI(
        azure_endpoint=base_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version
    )
    
    return client


def get_chat_client(project_client):
    """
    Get a chat completions client - just return the same client since it handles both.
    
    Args:
        project_client: The AzureOpenAI client instance
        
    Returns:
        The same Azure OpenAI client
    """
    return project_client


def get_embeddings_client(project_client):
    """
    Get an embeddings client - just return the same client since it handles both.
    
    Args:
        project_client: The AzureOpenAI client instance
        
    Returns:
        The same Azure OpenAI client
    """
    return project_client