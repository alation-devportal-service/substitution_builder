import streamlit as st
import os
import json
import re
import asyncio
import git
from github import Github
import google.generativeai as genai
import tempfile

# --- 1. SETUP & SECRETS ---
st.set_page_config(page_title="AI reST Substitution Builder", layout="wide")
st.title("🤖 AI reST Substitution Builder")

# Fetch secrets (Configured in Streamlit Cloud settings)
GITHUB_PAT = st.secrets.get("GITHUB_PAT")
# REPO_URL must be formatted as: github.com/yourorg/yourrepo.git (No https://)
REPO_URL = st.secrets.get("REPO_URL") 
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
# Use a persistent temp directory for Streamlit Cloud
REPO_DIR = os.path.join(tempfile.gettempdir(), "docs_repo")

# --- 2. GIT CLONE OPERATIONS ---
@st.cache_resource
def clone_repo():
    if not os.path.exists(REPO_DIR):
        st.info("Cloning repository... this might take a moment.")
        auth_url = f"https://oauth2:{GITHUB_PAT}@{REPO_URL}"
        git.Repo.clone_from(auth_url, REPO_DIR)
        st.success("Repo cloned successfully!")
    return git.Repo(REPO_DIR)

# --- 3. RECURSIVE SPHINX PARSER ---
def get_logical_chunks_recursive(current_dir, index_filename="index.rst", chunk_prefix=""):
    chunks = {}
    index_path = os.path.join(current_dir, index_filename)
    
    if not os.path.exists(index_path):
        return chunks
        
    chunk_name = chunk_prefix if chunk_prefix else "Root_Level"
    if chunk_name not in chunks:
        chunks[chunk_name] = []
    if index_path not in chunks[chunk_name]:
        chunks[chunk_name].append(index_path)

    in_toctree = False
    
    with open(index_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            
            if stripped.startswith(".. toctree::"):
                in_toctree = True
                continue
                
            if in_toctree:
                if not line.startswith(" ") and not line.startswith("\t") and stripped != "":
                    in_toctree = False
                    continue
                if stripped == "" or stripped.startswith(":"):
                    continue
                
                entry_path = stripped
                if entry_path.endswith('.rst'):
                    entry_path = entry_path[:-4]
                    
                full_target_path = os.path.normpath(os.path.join(current_dir, entry_path))
                sub_index = os.path.join(full_target_path, "index.rst")
                
                if os.path.basename(full_target_path) == "index" and os.path.exists(full_target_path + ".rst"):
                     dir_path = os.path.dirname(full_target_path)
                     sub_chunk_name = f"{chunk_prefix} > {os.path.basename(dir_path)}" if chunk_prefix else os.path.basename(dir_path)
                     chunks.update(get_logical_chunks_recursive(dir_path, "index.rst", sub_chunk_name))
                elif os.path.isdir(full_target_path) and os.path.exists(sub_index):
                    sub_chunk_name = f"{chunk_prefix} > {entry_path}" if chunk_prefix else entry_path
                    chunks.update(get_logical_chunks_recursive(full_target_path, "index.rst", sub_chunk_name))
                elif os.path.exists(full_target_path + ".rst"):
                    file_chunk_name = f"{chunk_prefix} > {entry_path}" if chunk_prefix else entry_path
                    if file_chunk_name not in chunks:
                        chunks[file_chunk_name] = []
                    chunks[file_chunk_name].append(full_target_path + ".rst")
                elif os.path.isdir(full_target_path):
                    dir_chunk_name = f"{chunk_prefix} > {entry_path}" if chunk_prefix else entry_path
                    chunks[dir_chunk_name] = []
                    for root, _, files in os.walk(full_target_path):
                        for file in files:
                            if file.endswith('.rst'):
                                chunks[dir_chunk_name].append(os.path.join(root, file))
    return chunks

# --- 4. ASYNC AI ANALYSIS ---
async def analyze_chunk_async(model, chunk_name, file_contents, semaphore):
    async with semaphore:
        prompt = f"""
        Analyze the following reStructuredText (reST) content from the docs section: '{chunk_name}'. 
        Identify repetitive phrases, UI navigation steps, or standard warnings that would make good reST substitutions.
        Return ONLY a valid JSON list of dictionaries with this format:
        [
          {{"tag": "|Suggested Tag Name|", "text": "The repetitive text to be replaced", "approved": false}}
        ]
        Content:
        {file_contents}
        """
        try:
            response = await model.generate_content_async(prompt)
            json_text = response.text.replace('```json', '').replace('```', '').strip()
            return json.loads(json_text)
        except Exception as e:
            print(f"Error analyzing chunk '{chunk_name}': {e}")
            return []

async def process_all_chunks_concurrently(logical_chunks):
    model = genai.GenerativeModel('gemini-2.5-pro')
    semaphore = asyncio.Semaphore(5) 
    tasks = []
    
    for chunk_name, file_paths in logical_chunks.items():
        combined_content = ""
        for path in set(file_paths): # Use set to avoid duplicates
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    combined_content += f"\n\n--- FILE: {os.path.basename(path)} ---\n" + f.read()
            except Exception as e:
                print(f"Error reading file {path}: {e}")
        
        if combined_content.strip():
            tasks.append(asyncio.create_task(analyze_chunk_async(model, chunk_name, combined_content, semaphore)))
    
    results = await asyncio.gather(*tasks)
    
    all_suggestions = []
    for res in results:
        if isinstance(res, list):
            all_suggestions.extend(res)
            
    # Deduplicate by 'text' mapping to prevent showing the exact same text twice
    unique_suggestions = {item['text']: item for item in all_suggestions if 'text' in item and len(item['text']) > 10}.values()
    return list(unique_suggestions)

# --- 5. SAFE REGEX WRITER ---
def apply_substitutions_safely(base_path, approved_items):
    sub_file_path = os.path.join(base_path, "substitutions.rst")
    with open(sub_file_path, 'a', encoding='utf-8') as f:
        f.write("\n\n.. Auto-generated AI Substitutions\n")
        for item in approved_items:
            f.write(f".. {item['tag']} replace:: {item['text']}\n")

    for root, _, files in os.walk(base_path):
        for file in files:
            if file.endswith('.rst') and file != "substitutions.rst":
                file_path = os.path.join(root, file)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                original_content = content
                for item in approved_items:
                    escaped_old_text = re.escape(item['text'])
                    content = re.sub(escaped_old_text, item['tag'], content)
                
                if content != original_content:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)

# --- 6. MAIN UI WORKFLOW ---
def main():
    try:
        repo = clone_repo()
    except Exception as e:
        st.error(f"Failed to clone repository. Check your PAT and REPO_URL. Error: {e}")
        st.stop()

    # Get top-level directories in the repo (ignoring hidden git folders)
    subdirs = [d for d in os.listdir(REPO_DIR) if os.path.isdir(os.path.join(REPO_DIR, d)) and not d.startswith('.')]
    selected_folder = st.selectbox("Select Project Folder to Analyze", ["/ (Root)"] + subdirs)

    target_path = REPO_DIR if selected_folder == "/ (Root)" else os.path.join(REPO_DIR, selected_folder)

    # STAGE 1: ANALYZE
    if st.button("1. Analyze .rst Files Concurrently"):
        with st.spinner("Mapping folders and analyzing with Gemini..."):
            try:
                logical_chunks = get_logical_chunks_recursive(target_path, "index.rst")
                if not logical_chunks:
                    st.warning("No index.rst found or no chunks generated. Falling back to simple file read.")
                    logical_chunks = {"Fallback_Chunk": [os.path.join(root, f) for root, _, files in os.walk(target_path) for f in files if f.endswith('.rst')]}
                    
                all_suggestions = asyncio.run(process_all_chunks_concurrently(logical_chunks))
                st.session_state['suggestions'] = all_suggestions
                st.success(f"Found {len(all_suggestions)} unique substitution candidates!")
            except Exception as e:
                st.error(f"Analysis failed: {e}")

    # STAGE 2 & 3: REVIEW AND PR
    if 'suggestions' in st.session_state and st.session_state['suggestions']:
        st.write("### 2. Review Suggested Substitutions")
        st.info("Check the 'approved' box for the tags you want to keep. Feel free to edit the tag names.")
        
        edited_df = st.data_editor(st.session_state['suggestions'], num_rows="dynamic", use_container_width=True)
        
        st.write("### 3. Version Control & Pull Request")
        
        # Dynamically fetch available branches from the remote repository
        try:
            remote_refs = repo.remote().refs
            available_branches = list(set([ref.name.replace('origin/', '') for ref in remote_refs if ref.name != 'origin/HEAD']))
            
            # Bring 'main' or 'master' to the top of the list as the default option
            if 'main' in available_branches:
                available_branches.insert(0, available_branches.pop(available_branches.index('main')))
            elif 'master' in available_branches:
                available_branches.insert(0, available_branches.pop(available_branches.index('master')))
        except Exception:
            # Safe fallback if fetching fails
            available_branches = ["main", "master"]

        # Use columns for a cleaner UI layout
        col1, col2 = st.columns(2)
        
        with col1:
            # 1. Let the user define the new branch name
            raw_branch_name = st.text_input("New Branch Name (Head)", value="feature/ai-docs-update")
            # Sanitize the branch name
            safe_branch_name = re.sub(r'[^a-zA-Z0-9.\-_/]', '', raw_branch_name.replace(" ", "-"))
            
            if safe_branch_name != raw_branch_name:
                st.caption(f"ℹ️ *Sanitized to:* `{safe_branch_name}`")

        with col2:
            # 2. Let the user select the target branch for the PR
            base_branch = st.selectbox("Target Branch (Base)", options=available_branches)

        # Execution Button
        if st.button("Apply Approved Substitutions & Create PR"):
            approved_items = [item for item in edited_df if item.get('approved') == True]
            
            if not approved_items:
                st.warning("No substitutions approved. Please check at least one box.")
            elif not safe_branch_name:
                 st.warning("Please provide a valid branch name.")
            else:
                with st.spinner(f"Applying changes and creating PR from `{safe_branch_name}` into `{base_branch}`..."):
                    try:
                        # Apply changes via Python Regex
                        apply_substitutions_safely(target_path, approved_items)
                        
                        # Git checkout & commit
                        branch_name = safe_branch_name
                        
                        if branch_name not in [b.name for b in repo.branches]:
                            repo.git.checkout('-b', branch_name)
                        else:
                            repo.git.checkout(branch_name)

                        repo.git.add(A=True)
                        repo.index.commit("docs: apply AI suggested reST substitutions")
                        
                        # Push to origin
                        origin = repo.remote(name='origin')
                        origin.push(refspec=f'{branch_name}:{branch_name}')
                        
                        # Create Pull Request via GitHub API
                        gh_repo_path = REPO_URL.replace("github.com/", "").replace(".git", "")
                        g = Github(GITHUB_PAT)
                        gh_repo = g.get_repo(gh_repo_path)
                        
                        pr_title = f"Docs: AI Suggested reST Substitutions from '{branch_name}'"
                        pr_body = (
                            "This PR was automatically generated by the AI reST Substitution Builder app.\n\n"
                            f"**Target Branch:** `{base_branch}`\n"
                            f"**Substitutions Applied:** {len(approved_items)}\n\n"
                            "Please review the changes to ensure the AI-suggested Regex replacements did not disrupt formatting."
                        )
                        
                        # Open the PR against the selected base branch
                        pr = gh_repo.create_pull(
                            title=pr_title, 
                            body=pr_body, 
                            head=branch_name, 
                            base=base_branch 
                        )
                        
                        st.success(f"🎉 Successfully applied substitutions, pushed branch, and created PR!")
                        st.markdown(f"**👉 [Click here to review your Pull Request]({pr.html_url})**")
                        
                        # Clear session state so user can start fresh
                        del st.session_state['suggestions']
                        
                    except Exception as e:
                        st.error(f"Git push or PR creation failed. Note: If no actual text was changed, Git will reject the push. Error: {e}")

if __name__ == "__main__":
    main()
