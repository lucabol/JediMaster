"""
Test script to verify connectivity to Azure AI Foundry agents.
Tests: DeciderAgent, PRDeciderAgent, and CreatorAgent
"""

import os
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential

# Load environment variables
load_dotenv()

FOUNDRY_PROJECT_ENDPOINT = os.getenv('AZURE_AI_FOUNDRY_PROJECT_ENDPOINT')

def test_foundry_connection():
    """Test basic connection to Azure AI Foundry project."""
    print("="*80)
    print("Testing Azure AI Foundry Agent Connection")
    print("="*80)
    print(f"\nFoundry Project Endpoint: {FOUNDRY_PROJECT_ENDPOINT}")
    
    if not FOUNDRY_PROJECT_ENDPOINT:
        print("❌ ERROR: AZURE_AI_FOUNDRY_PROJECT_ENDPOINT not set in .env")
        return False, [], []
    
    try:
        # Test authentication
        print("\n1. Testing authentication...")
        credential = DefaultAzureCredential()
        # Try to get a token for Azure AI services
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        print(f"✅ Authentication successful")
        print(f"   Token type: {type(token)}")
        print(f"   Token expires: {token.expires_on}")
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        return False, [], []
    
    # Test agent discovery/listing
    print("\n2. Testing agent discovery...")
    try:
        from azure.ai.projects import AIProjectClient
        
        # Create project client (synchronous)
        client = AIProjectClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=credential
        )
        
        print(f"✅ Created AIProjectClient")
        
        # Try to get agents by name
        print("\n3. Attempting to retrieve agents by name...")
        required_agents = ['DeciderAgent', 'PRDeciderAgent', 'CreatorAgent']
        found_agents = {}
        
        for agent_name in required_agents:
            try:
                # Use the pattern from sample code
                agent = client.agents.get(agent_name=agent_name)
                print(f"   ✅ Found {agent_name}: ID={agent.id}")
                found_agents[agent_name] = agent
            except Exception as e:
                print(f"   ⚠️  {agent_name} not found or error: {e}")
        
        print(f"\n   Successfully retrieved {len(found_agents)}/{len(required_agents)} agents")
        
        return True, found_agents, credential, client
            
    except ImportError as e:
        print(f"❌ Missing required package: {e}")
        print("   You may need to install: pip install azure-ai-projects azure-ai-agents")
        return False, {}, None, None
    except Exception as e:
        print(f"❌ Error creating client: {e}")
        import traceback
        traceback.print_exc()
        return False, {}, None, None
    
    print("\n" + "="*80)
    print("Connection test complete")
    print("="*80)

def test_agent_invocation(found_agents, credential, client):
    """Test invoking a specific agent using the sample code pattern."""
    print("\n" + "="*80)
    print("Testing Agent Invocation")
    print("="*80)
    
    if 'DeciderAgent' not in found_agents:
        print("❌ DeciderAgent not found, skipping invocation test")
        return
    
    try:
        agent = found_agents['DeciderAgent']
        print(f"\nTesting DeciderAgent (ID: {agent.id})...")
        
        # Get OpenAI client as shown in sample
        openai_client = client.get_openai_client()
        print(f"✅ Got OpenAI client: {type(openai_client)}")
        
        # Test with a simple issue evaluation request
        print("\nSending test request: 'Tell me what you can help with.'")
        
        response = openai_client.responses.create(
            input=[{"role": "user", "content": "Tell me what you can help with."}],
            extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
        )
        
        print(f"✅ Successfully invoked DeciderAgent!")
        print(f"\nResponse output: {response.output_text}")
        print(f"Response type: {type(response)}")
        
    except Exception as e:
        print(f"❌ Error invoking agent: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("\n🔍 Azure AI Foundry Agent Connectivity Test\n")
    
    # Run connection test
    success, found_agents, credential, client = test_foundry_connection()
    
    # If successful and we found agents, test invocation
    if success and len(found_agents) > 0:
        test_agent_invocation(found_agents, credential, client)
    
    print("\n✅ Test script complete. Check output above for any errors.")
