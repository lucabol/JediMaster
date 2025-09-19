#!/usr/bin/env python3
"""
Test CreatorAgent with a simple prompt to debug the empty response issue.
"""

import os
from dotenv import load_dotenv
from azure_ai_foundry_utils import create_azure_ai_foundry_client, get_chat_client

# Load environment variables
load_dotenv()

def test_json_response():
    """Test JSON response format with Azure AI Foundry."""
    
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
        
        # Test JSON response format with a simple prompt
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Always respond in JSON format."},
            {"role": "user", "content": "Generate 2 simple GitHub issues as a JSON array. Each issue should have 'title' and 'body' fields."}
        ]
        
        print(f"Making request with model: {model}")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )
        
        print(f"Response details:")
        print(f"  Finish reason: {response.choices[0].finish_reason}")
        print(f"  Content length: {len(response.choices[0].message.content or '')}")
        print(f"  Usage: {response.usage}")
        
        content = response.choices[0].message.content
        if content:
            print(f"✅ Success! Response: {content}")
        else:
            print("❌ Empty response from model")
            print(f"Full response: {response}")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    test_json_response()