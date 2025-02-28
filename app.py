import streamlit as st
from langchain_community.agent_toolkits.sql.base import create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain_community.callbacks.streamlit import StreamlitCallbackHandler
from langchain.agents.agent_types import AgentType
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from sqlalchemy import create_engine, text
import psycopg2
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_groq import ChatGroq
from langchain_postgres import PGVector
from pgvector.sqlalchemy import Vector
from langchain.schema import Document
import os
import torch
import asyncio
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up async event loop fix
torch.classes.__path__ = [os.path.join(torch.__path__[0], torch.classes.__file__)]
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Set API Keys
os.environ['HF_TOKEN'] = os.getenv("HF_TOKEN")

# Load Embedding Model
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-mpnet-base-v2")

# Streamlit UI Setup
st.set_page_config(page_title="LangChain: Hybrid Search with PostgreSQL", page_icon="🦜")
st.title("🦜 LangChain: Hybrid Search with PostgreSQL Database")

# Sidebar Configuration
with st.sidebar:
    st.header("Configuration")
    api_key = st.text_input("Groq API Key", type="password")

    st.subheader("Database Connection")
    postgres_host = st.text_input("Host", value="localhost")
    postgres_user = st.text_input("User", value="langchain")
    postgres_password = st.text_input("Password", type="password", value="langchain")
    postgres_db = st.text_input("Database", value="langchain")
    postgres_port = st.text_input("Port", value="6024")

    if st.button("Refresh Vector Embeddings"):
        st.session_state["refresh_embeddings"] = True

if not all([postgres_host, postgres_user, postgres_password, postgres_db, postgres_port]):
    st.info("Please provide all PostgreSQL connection details.")

@st.cache_resource(ttl="2h")
def configure_db():
    connection_string = f"postgresql+psycopg2://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/{postgres_db}"
    return SQLDatabase.from_uri(connection_string), connection_string

# Initialize Database
db, connection_string = configure_db()
connection = "postgresql+psycopg://langchain:langchain@localhost:6024/langchain"
collection_name = "my_docs"

vector_store = PGVector(
    embeddings=embeddings,
    collection_name=collection_name,
    connection=connection,
    use_jsonb=True,
)

def execute_sql_command(command):
    try:
        engine = create_engine(connection_string)
        with engine.connect() as conn:
            conn.execute(text(command))
            conn.commit()
            conn.close()
        return "Query executed successfully!"
    except Exception as e:
        return f"Error executing query: {str(e)}"
    
# Add a checkbox to show/hide the "Create or Alter Tables" section
show_sql_editor = st.checkbox("Enable SQL Table Editor")

# Show the section only if the checkbox is checked
if show_sql_editor:
    st.subheader("Create or Alter Tables")
    sql_command = st.text_area("Enter SQL Query (CREATE TABLE / ALTER TABLE)", height=150)
    
    if st.button("Execute SQL Command"):
        result = execute_sql_command(sql_command)
        st.write(result)

# Function to Populate Vector Store
def populate_vector_store(db, vector_store, tables=["employees", "departments", "products", "orders"]):
    documents = []
    engine = create_engine(db._engine.url)
    
    with engine.connect() as conn:
        for table in tables:
            result = conn.execute(text(f"SELECT * FROM {table}"))
            for row in result:
                content = f"Table: {table}\n" + "\n".join(f"{col}: {val}" for col, val in zip(result.keys(), row))
                doc = Document(page_content=content, metadata={"table": table, "id": str(row[0])})
                
                # Check if document already exists
                existing_docs = vector_store.similarity_search(doc.page_content, k=1)
                if not existing_docs:  
                    documents.append(doc)
    
    if documents:
        vector_store.add_documents(documents)
        return len(documents)
    return 0

# Initialize SQL Agent
toolkit = SQLDatabaseToolkit(db=db, llm=ChatGroq(groq_api_key=api_key, model_name="Llama3-8b-8192", streaming=True))
sql_agent = create_sql_agent(
    llm=toolkit.llm,
    toolkit=toolkit,
    verbose=True,
    agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
    handle_parsing_errors=True
)

# Refresh Vector Store if needed
if st.session_state.get("refresh_embeddings", False):
    with st.spinner("Refreshing vector embeddings..."):
        count = populate_vector_store(db, vector_store)
        st.success(f"Updated {count} new documents in vector store.")
        st.session_state["refresh_embeddings"] = False

# Hybrid Search Function
def run_hybrid_search(query):
    try:
        vector_results = vector_store.similarity_search(query, k=3)
        context = "\n\n".join([doc.page_content for doc in vector_results])

        enhanced_prompt = f"""Use both vector search results and SQL to answer the question.
        
        Vector search context:
        {context}
        
        Now use SQL queries as needed to provide a complete answer to: {query}
        
        Be thorough but concise. Combine both sources.
        """
        
        return sql_agent.run(enhanced_prompt)

    except Exception as e:
        return f"An error occurred: {str(e)}"


# Manage Chat History
if "messages" not in st.session_state or st.sidebar.button("Clear message history"):
    st.session_state["messages"] = [{"role": "assistant", "content": "How can I help you?"}]

for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

# User Input
user_query = st.chat_input("Ask anything from the PostgreSQL database")

if user_query:
    st.session_state.messages.append({"role": "user", "content": user_query})
    st.chat_message("user").write(user_query)
    
    with st.chat_message("assistant"):
        streamlit_callback = StreamlitCallbackHandler(st.container())
        try:
            response = run_hybrid_search(user_query)
            st.session_state.messages.append({"role": "assistant", "content": response})
            st.write(response)
        except Exception as e:
            error_msg = f"An error occurred: {str(e)}"
            st.error(error_msg)
            st.session_state.messages.append({"role": "assistant", "content": error_msg})