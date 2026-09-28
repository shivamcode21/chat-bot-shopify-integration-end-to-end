import streamlit as st
from langchain.chat_models import ChatOpenAI
from langchain.chains import ConversationalRetrievalChain
from langchain.vectorstores import FAISS
from langchain.embeddings import OpenAIEmbeddings
from langchain.prompts import PromptTemplate
from tools import mcp
from fastmcp import Client
import re, asyncio, json
import logging

# Load FAISS index
db = FAISS.load_local("fashion_bot/fashion_bot/memory", OpenAIEmbeddings(), allow_dangerous_deserialization=True)
retriever = db.as_retriever()

# Custom prompt template with system context
custom_prompt_template = """You are a helpful fashion support assistant for an online clothing store. 

**Your Role:**
- Help customers with order inquiries, returns, product questions, and general fashion advice
- Be friendly, professional, and empathetic
- Provide accurate information about orders, returns, and products
- If you don't have specific information, guide customers to the right tools or human support
- Personalize responses based on customer context

**Customer Context:**
- Name: {customer_name}

**Available Information:**
{context}

**Current Conversation:**
{chat_history}

**Customer Question:** {question}

**Your Response:**"""

# Create custom prompt
prompt = PromptTemplate(
    input_variables=["context", "chat_history", "question", "customer_name"],
    template=custom_prompt_template
)

# Setup retrieval chain with custom prompt
llm = ChatOpenAI(model="gpt-4o", temperature=0)
chain = ConversationalRetrievalChain.from_llm(
    llm=llm, 
    retriever=retriever,
    combine_docs_chain_kwargs={"prompt": prompt},
    return_source_documents=True  # This will show which documents were retrieved
)

# App title
st.title("🛍️ Groovee Fashion Support GenAI Chatbot")

# Initialize chat history
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# Initialize user context
if "user_context" not in st.session_state:
    st.session_state.user_context = {
        "preferences": {},
        "recent_orders": []
    }

# Sidebar for additional context
with st.sidebar:
    st.header("🔧 Context Settings")
    
    # User preferences
    st.subheader("User Preferences")
    user_name = st.text_input("Customer Name", value=st.session_state.user_context.get("name", ""))
    if user_name:
        st.session_state.user_context["name"] = user_name
    
    # Manual context injection
    # st.subheader("Add Context")
    # additional_context = st.text_area(
    #     "Additional Context (e.g., special instructions, customer notes)",
    #     value=st.session_state.user_context.get("additional_context", "")
    # )
    # if additional_context:
    #     st.session_state.user_context["additional_context"] = additional_context

# Display previous messages
for user_msg, bot_msg in st.session_state.chat_history:
    with st.chat_message("user"):
        st.write(user_msg)
    with st.chat_message("assistant"):
        st.write(bot_msg)

# Get user input
user_input = st.chat_input("Ask your question...")

if user_input:
    # ✅ Show latest user message immediately
    with st.chat_message("user"):
        st.write(user_input)

    async def process_input():
        async with Client(mcp) as client:
            # Step 1: Frustration detection
            fr = await client.call_tool("detect_frustration", {"message": user_input})
            fr_data = json.loads(str(getattr(fr[0], 'text', fr[0])))
            if fr_data.get("frustrated"):
                response = "I'm escalating you to a human support agent. Please hold on..."
                with st.chat_message("assistant"):
                    st.write(response)
                st.session_state.chat_history.append((user_input, response))
                return

            # Step 2: Tool-based pattern recognition
            if user_input and isinstance(user_input, str):
                if m := re.search(r"gv\d+", user_input):
                    res = await client.call_tool("get_order_status", {"order_id": m.group()})
                    data = json.loads(str(getattr(res[0], 'text', res[0])))
                    response = f"📦 Order Info: {data}"
                    with st.chat_message("assistant"):
                        st.write(response)
                    logging.warning(f"Order tool response: {data}")
                    st.session_state.chat_history.append((user_input, response))
                    return

                elif m := re.search(r"RET\d+", user_input):
                    res = await client.call_tool("get_return_status", {"return_id": m.group()})
                    data = json.loads(str(getattr(res[0], 'text', res[0])))
                    response = f"🔁 Return Info: {data}"
                    with st.chat_message("assistant"):
                        st.write(response)
                    st.session_state.chat_history.append((user_input, response))
                    return

                elif m := re.search(r"SKU\d+", user_input):
                    res = await client.call_tool("get_product_details", {"sku": m.group()})
                    data = json.loads(str(getattr(res[0], 'text', res[0])))
                    response = f"👗 Product Info: {data}"
                    with st.chat_message("assistant"):
                        st.write(response)
                    st.session_state.chat_history.append((user_input, response))
                    return

            # Step 3: Fallback to RAG with enhanced context
            result = chain({
                "question": user_input,
                "chat_history": st.session_state.chat_history,
                "customer_name": st.session_state.user_context.get("name", "Customer"),
                # "additional_context": st.session_state.user_context.get("additional_context", "")
            })
            response = result["answer"]
            
            # Optionally show source documents for debugging
            # if st.checkbox("Show source documents (debug)"):
            #     st.write("**Source Documents:**")
            #     for i, doc in enumerate(result.get("source_documents", [])):
            #         st.write(f"Source {i+1}: {doc.page_content[:200]}...")
            
            with st.chat_message("assistant"):
                st.write(response)
            st.session_state.chat_history.append((user_input, response))

    # Run async
    asyncio.run(process_input())
