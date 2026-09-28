import json
from langchain.vectorstores import FAISS
from langchain.embeddings import OpenAIEmbeddings

def load_and_chunk_conversations():
    with open("chat_history.json") as f:
        data = json.load(f)
    chunks = []
    for conv in data:
        full = "\n".join([f"User: {m['user']}\nAssistant: {m['assistant']}" for m in conv["messages"]])
        chunks.append(full)
    return chunks

def build_faiss():
    chunks = load_and_chunk_conversations()
    embeddings = OpenAIEmbeddings()
    db = FAISS.from_texts(chunks, embeddings)
    db.save_local("memory/")

if __name__ == "__main__":
    build_faiss()
