#!/usr/bin/env python3
"""
Debug script to test Azure AI Foundry authentication and model deployment.
"""

import os
from dotenv import load_dotenv
from azure_ai_foundry_utils import create_azure_ai_foundry_client, get_chat_client

# Load environment variables
load_dotenv()

def test_chat_completion():
    """Test a simple chat completion with Azure AI Foundry."""
    
    # Get endpoint from environment
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')
    model = os.getenv('AZURE_AI_MODEL', 'model-router')
    
    print(f"Endpoint: {azure_foundry_endpoint}")
    print(f"Model: {model}")
    
    if not azure_foundry_endpoint:
        print("Error: AZURE_AI_FOUNDRY_ENDPOINT not set")
        return
    
    try:
        # Create client
        project_client = create_azure_ai_foundry_client(azure_foundry_endpoint)
        client = get_chat_client(project_client)
        print("✅ Client created successfully")
        
        # Test a simple chat completion
        messages = [
            {"role": "user", "content": "Hello"}
        ]
        
        print(f"Making request with model: {model}")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=100
        )
        
        print(f"Response: {response}")
        print(f"Finish reason: {response.choices[0].finish_reason}")
        print(f"Content: '{response.choices[0].message.content}'")
        print(f"Usage: {response.usage}")
        
        if response.choices and response.choices[0].message.content:
            print(f"✅ Success! Response: {response.choices[0].message.content}")
        else:
            print("❌ Empty response from model")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    test_chat_completion()