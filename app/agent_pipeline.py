import os
from sqlalchemy.orm import Session
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

# Import models from your FastAPI application schema
from app.models.employee import Employee
from app.models.task import OnboardingTask  # Replace with your actual task model name

# ==========================================
# 1. SECRETLESS AUTHENTICATION
# ==========================================
azure_credential = DefaultAzureCredential()

def get_openai_token_provider():
    """Generates dynamic Entra ID access tokens for Azure OpenAI."""
    return get_bearer_token_provider(
        azure_credential, "https://cognitiveservices.azure.com/.default"
    )

# ==========================================
# 2. STRUCTURED SQL CONTEXT INJECTION
# ==========================================
def get_user_sql_context(db: Session, employee_id: str) -> dict:
    """
    Queries Azure SQL / local SQLite to get role, department, and pending tasks.
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    
    if not employee:
        return {
            "name": "Employee",
            "role": "General Staff",
            "department": "General",
            "pending_tasks": []
        }
    
    # Fetch pending tasks for personalized prompt context
    pending_tasks = db.query(OnboardingTask).filter(
        OnboardingTask.employee_id == employee_id,
        OnboardingTask.is_completed == False
    ).all()

    return {
        "name": getattr(employee, "full_name", "Employee"),
        "role": getattr(employee, "job_title", "Team Member"),
        "department": getattr(employee, "department_name", "General"),
        "pending_tasks": [task.title for task in pending_tasks] if pending_tasks else []
    }

# ==========================================
# 3. UNSTRUCTURED VECTOR RETRIEVAL (AZURE AI SEARCH)
# ==========================================
def search_policy_documents(query: str, openai_client: AzureOpenAI, top_k: int = 3) -> str:
    """
    Embeds the user query and searches the Azure AI Search policy index.
    """
    search_endpoint = os.getenv("SEARCH_ENDPOINT")
    index_name = os.getenv("POLICY_INDEX_NAME", "policy-index")
    embedding_deployment = os.getenv("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")

    if not search_endpoint:
        return "Policy search endpoint is not configured."

    try:
        # Embed the incoming query
        query_vector = openai_client.embeddings.create(
            input=query,
            model=embedding_deployment
        ).data[0].embedding

        # Initialize AI Search Client using Managed Identity
        search_client = SearchClient(
            endpoint=search_endpoint,
            index_name=index_name,
            credential=azure_credential
        )

        vector_query = VectorizedQuery(
            vector=query_vector, 
            k_nearest_neighbors=top_k, 
            fields="content_vector"
        )
        
        results = search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            top=top_k
        )
        
        chunks = [doc["content"] for doc in results if "content" in doc]
        return "\n---\n".join(chunks) if chunks else "No matching policy documents found."

    except Exception as e:
        print(f"[Warning] Vector search degraded or failed: {e}")
        return "Policy documentation retrieval is currently unavailable."

# ==========================================
# 4. MAIN AGENT EXECUTION FUNCTION
# ==========================================
def run_ai_agent(user_query: str, user_id: str, db: Session) -> str:
    """
    Main RAG Agent Execution Loop:
    Combine SQL Context + AI Search Vector Context -> Azure OpenAI Prompt.
    """
    openai_endpoint = os.getenv("OPENAI_ENDPOINT")
    chat_deployment = os.getenv("CHAT_DEPLOYMENT", "gpt-5")

    # 1. Fetch Structured Context from SQL
    context = get_user_sql_context(db, user_id)

    # 2. Setup OpenAI Client with Entra ID
    token_provider = get_openai_token_provider()
    openai_client = AzureOpenAI(
        azure_endpoint=openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-02-01"
    )

    # 3. Retrieve Unstructured Context from Azure AI Search
    policy_context = search_policy_documents(user_query, openai_client)

    # 4. Construct System Prompt
    system_prompt = f"""
You are the internal AI Assistant for employee queries and company policy support.

User Context:
- Name: {context['name']}
- Role: {context['role']}
- Department: {context['department']}
- Pending Tasks: {', '.join(context['pending_tasks']) if context['pending_tasks'] else 'None'}

Internal Policy Excerpts:
{policy_context}

Instructions:
Answer the employee's request using the internal policy excerpts and personalize your answer based on their user context.
"""

    # 5. Call LLM
    response = openai_client.chat.completions.create(
        model=chat_deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
        ],
        temperature=0.2
    )

    return response.choices[0].message.content

# ==========================================
# 5. LOCAL TEST HARNESS
# ==========================================
if __name__ == "__main__":
    """
    Run this file directly to test if everything executes properly before sharing:
    python app/agent.py
    """
    print("Testing AI Agent execution locally...")
    
    from app.db import SessionLocal  # Import database session builder
    
    test_db = SessionLocal()
    test_user_id = "1"  # Replace with a valid ID present in your seed data
    test_query = "What is the policy for taking leave during onboarding?"

    try:
        response = run_ai_agent(
            user_query=test_query,
            user_id=test_user_id,
            db=test_db
        )
        print("\n--- Agent Execution Successful ---")
        print("Response:\n", response)
    except Exception as err:
        print("\n--- Agent Execution Failed ---")
        print("Error:", err)
    finally:
        test_db.close()
