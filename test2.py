#Import Library
from unstructured.partition.pdf import partition_pdf
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain.schema.runnable import RunnablePassthrough,RunnableLambda

from langchain.vectorstores import Chroma
# from langchain_chroma import Chroma
# from langchain.storage import InMemoryStore


# from langchain_postgres.vectorstores import PGVector
# from langchain.vectorstores.pgvector import PGVector
from langchain_postgres.vectorstores import PGVector
from database import COLLECTION_NAME, CONNECTION_STRING
from langchain_community.utilities.redis import get_client
from langchain_community.storage import RedisStore
from langchain.schema.document import Document
from langchain_openai import OpenAIEmbeddings
from langchain.retrievers.multi_vector import MultiVectorRetriever
from pathlib import Path
from IPython.display import display, HTML
from base64 import b64decode
import hashlib
import chromadb
import tempfile
import shutil
import streamlit as st
import logging
import uuid
import json
import time
import torch
import redis
import os
from dotenv import load_dotenv
load_dotenv()


torch.classes.__path__ = [os.path.join(torch.__path__[0], torch.classes.__file__)] 

PERSIST_DIRECTORY = os.path.join("data", "vectors")

FILE_PATH = Path("data/hbspapers_48__1.pdf") 


logging.basicConfig(level=logging.INFO)

r = redis.Redis(host="localhost", port=6379, db=0)




###Data Loading
def load_pdf_data(file_path):
    logging.info(f"Data ready to be partitioned and loaded ")
    raw_pdf_elements = partition_pdf(
        filename=file_path,
      
        infer_table_structure=True,
        strategy = "hi_res",
        
        extract_image_block_types = ["Image"],
        extract_image_block_to_payload  = True,

        chunking_strategy="by_title",     
        mode='elements',
        max_characters=10000,
        new_after_n_chars=5000,
        combine_text_under_n_chars=2000,
        image_output_dir_path="data/",
    )
    logging.info(f"Pdf data finish loading, chunks now available!")
    return raw_pdf_elements


#summarize the data
def summarize_text_and_tables(text, tables):
    logging.info("Ready to summarize data with LLM")
    llm_summary= {}
    prompt_text = """You are an assistant tasked with summarizing text and tables. \
    
                    You are to give a concise summary of the table or text and do nothing else. 
                    Table or text chunk: {element} """
    prompt = ChatPromptTemplate.from_template(prompt_text)
    model = ChatOpenAI(temperature=0.6, model="gpt-4o-mini")
    summarize_chain = {"element": RunnablePassthrough()}| prompt | model | StrOutputParser()
    # table_summary = summarize_chain.batch(tables, {"max_concurrency": 5})
    # text_summary = summarize_chain.batch(text, {"max_concurrency": 5})
    # llm_summary['text'] = text_summary
    # llm_summary['table'] = table_summary
    #return llm_summary
    logging.info(f"{model} done with summarization")
    return {
        "text": summarize_chain.batch(text, {"max_concurrency": 5}),
        "table": summarize_chain.batch(tables, {"max_concurrency": 5})
    }
  

###Multivector Retriever

def create_retriever(documents, summaries):
    """Creates a multi-vector retriever and adds documents."""
    client = get_client("redis://localhost:6379")
    store = RedisStore(client=client)
    id_key = "doc_id"

    vectorstore = PGVector(
        embeddings=OpenAIEmbeddings(),
        collection_name=COLLECTION_NAME,
        connection=CONNECTION_STRING,
        use_jsonb=True,
        )
    retriever = MultiVectorRetriever(vectorstore=vectorstore, docstore=store, id_key="doc_id")
    return retriever

    def add_documents(documents, summaries):
        """Helper function to add documents and summaries to the retriever."""
        if not summaries:
            return None, []
        
        doc_ids = [str(uuid.uuid4()) for _ in documents]
        summary_docs = [
            Document(page_content=summary, metadata={id_key: doc_ids[i]})
            for i, summary in enumerate(summaries)
        ]

        # vectorstore.add_documents(documents=summary_docs, ids=doc_ids)
        retriever.vectorstore.add_documents(summary_docs, ids=doc_ids)
        retriever.docstore.mset(list(zip(doc_ids, documents)))
         

    add_documents(documents, summaries)
 
    logging.info("Retriever setup complete.")
    return retriever



# def create_retriever(documents, summaries):
#     """
#     Creates a MultiVectorRetriever by storing document summaries in a vectorstore
#     and mapping original documents to their unique IDs in a docstore.

#     :param documents: List of original documents.
#     :param summaries: Optional list of summaries corresponding to documents.
#     :param collection_name: Name of the PGVector collection.
#     :param connection_string: Connection string for PGVector.
#     :param id_key: Metadata key for document IDs.
#     :return: Configured MultiVectorRetriever instance.
#     """
    
#     client = get_client("redis://localhost:6379")
#     store = RedisStore(client=client)
#     id_key = "doc_id"
    
   

#     def add_vectors_to_db(documents, summaries):
#         """Helper function to store summaries as vector embeddings."""
#         if not summaries:
#             return None, []
        
#         doc_ids = [str(uuid.uuid4()) for _ in documents]
#         summary_docs = [
#             Document(page_content=summary, metadata={id_key: doc_ids[i]})
#             for i, summary in enumerate(summaries)
#         ]

        
#         vectorstore = PGVector(
#         embeddings=OpenAIEmbeddings(),
#         collection_name=COLLECTION_NAME,
#         connection=CONNECTION_STRING,
#         use_jsonb=True,
#         )

#         # vectorstore = PGVector.from_documents(
#         #     documents=summary_docs,
#         #     embedding=OpenAIEmbeddings(),
#         #     collection_name=COLLECTION_NAME,
#         #     connection_string=CONNECTION_STRING,
#         #     use_jsonb=True
#         # )
  

#         vectorstore.add_documents(documents=summary_docs, ids=doc_ids)
        
#         return vectorstore, doc_ids

#     vectorstore, doc_ids = add_vectors_to_db(documents, summaries)

#     # Ensure a valid vectorstore is passed
#     if not vectorstore:
#         raise ValueError("No summaries provided; cannot create vectorstore.")

#     retriever = MultiVectorRetriever(
#         vectorstore=vectorstore,
#         docstore=store,
#         id_key=id_key
#     )

#     # Store original documents in the docstore
#     if doc_ids:
#         retriever.docstore.mset(list(zip(doc_ids, documents)))

#     return retriever



###RAG pipeline
def parse_retriver_output(data):
    parsed_elements = []
    for element in data:
        if 'CompositeElement' in str(type(element)):
            parsed_elements.append(element.text)
        else:
            parsed_elements.append(element)
            
    return parsed_elements


###Chat with LLM

def chat_with_llm(retriever):

    logging.info(f"Context ready to send to LLM ")
    prompt_text = """
                You are an AI Assistant tasked with understanding detailed
                information from text and tables. You are to answer the question based on the 
                context provided to you. You must not go beyond the context given to you.
                
                Context:
                {context}

                Question:
                {question}
                """

    prompt = ChatPromptTemplate.from_template(prompt_text)
    model = ChatOpenAI(temperature=0.6, model="gpt-4o-mini")
 
#     rag_chain = (
#     {
#         "context": retriever | RunnableLambda(parse_retriver_output), 
#         "question": RunnablePassthrough()
#     }
#     | prompt
#     | model
#     | StrOutputParser()
# )
    rag_chain = ({
       "context": retriever | RunnableLambda(parse_retriver_output), "question": RunnablePassthrough(),
        } 
        | prompt 
        | model 
        | StrOutputParser()
        )
        
    logging.info(f"Completed! ")

    return rag_chain

### extract tables and text

def pdf_to_retriever(file_path):
    pdf_elements = load_pdf_data(file_path)
    
    tables = [element.metadata.text_as_html for element in
               pdf_elements if 'Table' in str(type(element))]
    
    text = [element.text for element in pdf_elements if 
            'CompositeElement' in str(type(element))]
   


    summaries = summarize_text_and_tables(text, tables)



    all_docs=text + tables
    text_summary = summaries['text']
    all_summaries = text_summary + summaries['table']


    retriever = create_retriever(all_docs, all_summaries)


    # retriever = create_retriever(text, summaries['text'], tables, summaries['table'])
    query = "What is the comparison of the composition of red meat and vegetarian protein sources"
    docs = retriever.invoke(query)
    check = [i for i in docs]
    
    return retriever



def invoke_chat(file_path, message):
    retriever = load__vectors(file_path)
    # retriever = pdf_to_retriever(file_path)
    rag_chain = chat_with_llm(retriever)
    response = rag_chain.invoke(message)
    response_placeholder = st.empty()
    response_placeholder.write(response)
    return response

# Function to get full PDF from Redis
def fetch_full_pdf(pdf_hash):
    pdf_data = r.get(f"pdf:{pdf_hash}")
    # return pdf_data
    return json.loads(pdf_data)["text"] if pdf_data else None


def get_pdf_hash(pdf_path):
    """Generate a SHA-256 hash of the PDF file content."""
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    return hashlib.sha256(pdf_bytes).hexdigest()

def old_retriever(pdf_hash):
    pdf_text = fetch_full_pdf(pdf_hash)
    if pdf_text is None:
        print("Error: PDF not found in Redis!")
        return None

    vectorstore = PGVector(
        embeddings=OpenAIEmbeddings(),
        collection_name=COLLECTION_NAME,
        connection=CONNECTION_STRING
    )
    # Create MultiVectorRetriever
    client = get_client("redis://localhost:6379")
    store = RedisStore(client=client)
    # id_key = "doc_id"


    # retriever = MultiVectorRetriever(
    #         vectorstore=vectorstore,
    #         docstore=store,
    #         id_key=id_key
    #     )
    retriever = MultiVectorRetriever(
        vectorstore=vectorstore,
        docstore=store,
        id_key = "doc_id"  # Pass function, NOT the result
    )
    return retriever


def load__vectors(file_path):
    print('Processing PDF hash info...')
    pdf_hash = get_pdf_hash(file_path)

    # Debug: Check if Redis already has the key
    existing = r.exists(f"pdf:{pdf_hash}")
    print(f"Checking Redis for hash {pdf_hash}: {'Exists' if existing else 'Not found'}")

    if existing:
        print(f"PDF already exists with hash {pdf_hash}. Skipping upload.")
        return old_retriever(pdf_hash)

    print(f"New PDF detected. Processing... {pdf_hash}")

    retriever = pdf_to_retriever(file_path)

    # Store the PDF hash in Redis
    r.set(f"pdf:{pdf_hash}", json.dumps({"text": "PDF processed"}))  

    # Debug: Check if Redis stored the key
    stored = r.exists(f"pdf:{pdf_hash}")
    print(f"Stored PDF hash in Redis: {'Success' if stored else 'Failed'}")

    return retriever







def main():
    # streamlit_initialize()
    st.title("PDF Chat Assistant ")
    logging.info("App started")

    if 'messages' not in st.session_state:
        st.session_state.messages = []

    # if "vector_db" not in st.session_state:
    #     st.session_state["vector_db"] = None
    
     # Create layout
    
    # file_upload = st.sidebar.file_uploader(
    # label="Upload", type=["pdf"], 
    # accept_multiple_files=False,
    # key="pdf_uploader"
    # )

    # if file_upload:
    #     st.success("File uploaded successfully! Processing...")
        
    #     # Uncomment and ensure `create_vector_db` is implemented if required
    #     # st.session_state["vector_db"] = create_vector_db(file_upload)
        
    #     st.success("File processed successfully! You can now ask your question.")

    # Prompt for user input
    if prompt := st.chat_input("Your question"):
        st.session_state.messages.append({"role": "user", "content": prompt})

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    # Generate response if last message is not from assistant
    if st.session_state.messages and st.session_state.messages[-1]["role"] != "assistant":
        with st.chat_message("assistant"):
            start_time = time.time()
            logging.info("Generating response...")
            with st.spinner("Writing..."):
                user_message = " ".join([msg["content"] for msg in st.session_state.messages if msg])
                
                # Ensure `invoke_chat` handles file uploads properly
                response_message = invoke_chat(FILE_PATH,user_message)

                duration = time.time() - start_time
                response_msg_with_duration = f"{response_message}\n\nDuration: {duration:.2f} seconds"

                st.session_state.messages.append({"role": "assistant", "content": response_msg_with_duration})
                st.write(f"Duration: {duration:.2f} seconds")
                logging.info(f"Response: {response_message}, Duration: {duration:.2f} s")


    



if __name__ == "__main__":
    main()