import streamlit as st
import os
import json
import re
import asyncio
import git
from github import Github
import google.generativeai as genai
import tempfile
import hashlib
import shutil

# --- 1. SETUP & SECRETS ---
st.set_page_config(page_title="Alation Substitution Builder", layout="wide")
st.title("Alation Substitution Builder")

# Fetch REPO_URL from secrets.
REPO_URL = st.secrets.get("REPO_URL", "github.com/your-org/your-repo.git")

# Validate REPO_URL to prevent silent cloning failures
if "your-org/your-repo" in REPO_URL:
    st.error("🚨 Configuration Error: The `REPO_URL` is set to the default placeholder. Please update your Streamlit Secrets with your actual repository URL.")
    st.stop()

# --- REGEX TO ISOLATE SPHINX META BLOCKS ---
META_BLOCK_REGEX = re.compile(r'(^[ \t]*\.\. meta::[ \t]*(?:\n|$)(?:[ \t]+.*(?:\n|$)|[ \t]*(?:\n|$))*)', re.MULTILINE)

# --- 2. RECURSIVE SPHINX PARSER ---
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
                    
                if stripped == "" or stripped.startswith(":") or stripped.startswith(".."):
                    continue
                
                entry_path = stripped
                match = re.search(r'<(.*?)>', entry_path)
                if match:
                    entry_path = match.group(1).strip()
                    
                if entry_path.endswith('.rst'):
                    entry_path = entry_path[:-4]
                    
                full_target_path = os.path.normpath(os.path.join(current_dir, entry_path))
                sub_index = os.path.join(full_target_path, "index.rst")
                
                if os.path.basename(full_target_path) == "index" and os.path.exists(full_target_path + ".rst"):
                     dir_path = os.path.dirname(full_target_path)
                     sub_chunk_name = f"{chunk_prefix} > {os.path.basename(dir_path)}" if chunk_prefix else os.path.basename(dir_path)
                     chunks.update(get_logical_chunks_recursive(dir_path, "index.rst", sub_chunk_name))
                elif os.path.isdir(full_target_path) and os.path.exists(sub_index):
                    sub_chunk_name = f"{chunk_prefix} > {os.path.basename(entry_path)}" if chunk_prefix else os.path.basename(entry_path)
                    chunks.update(get_logical_chunks_recursive(full_target_path, "index.rst", sub_chunk_name))
                elif os.path.exists(full_target_path + ".rst"):
                    file_chunk_name = f"{chunk_prefix} > {os.path.basename(entry_path)}" if chunk_prefix else os.path.basename(entry_path)
                    if file_chunk_name not in chunks:
                        chunks[file_chunk_name] = []
                    chunks[file_chunk_name].append(full_target_path + ".rst")
                elif os.path.isdir(full_target_path):
                    dir_chunk_name = f"{chunk_prefix} > {os.path.basename(entry_path)}" if chunk_prefix else os.path.basename(entry_path)
                    chunks[dir_chunk_name] = []
                    for root, _, files in os.walk(full_target_path):
                        for file in files:
                            if file.endswith('.rst'):
                                chunks[dir_chunk_name].append(os.path.join(root, file))
    return chunks

# --- 3. ASYNC AI ANALYSIS ---
async def analyze_chunk_async(model, chunk_name, file_contents, semaphore):
    async with semaphore:
        prompt = f"""
        Analyze the following reStructuredText (reST) content from the docs section: '{chunk_name}'. 
        Identify repetitive INLINE phrases or UI navigation steps that would make good reST substitutions.
        
        CRITICAL RULES:
        1. DO NOT select multi-line blocks, paragraphs, or numbered lists.
        2. The 'text' value MUST be a single, flat string with no line breaks (\\n).
        3. Capture WHOLE sentences or complete logical phrases including their preceding/trailing punctuation. Do not leave orphaned commas or periods behind.
        
        Return ONLY a JSON list of dictionaries with this exact format:
        [
          {{"tag": "|Suggested Tag Name|", "text": "The repetitive text to be replaced", "approved": false}}
        ]
        
        Content:
        {file_contents}
        """
        try:
            response = await model.generate_content_async(
                prompt,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json"
                )
            )
            return json.loads(response.text)
        except Exception as e:
            st.toast(f"⚠️ Error analyzing chunk '{chunk_name}': {e}")
            return []

async def process_all_chunks_concurrently(logical_chunks):
    model = genai.GenerativeModel('gemini-2.5-pro') 
    semaphore = asyncio.Semaphore(5) 
    tasks = []
    
    # 1MB file size limit to prevent OOM errors
    MAX_FILE_SIZE = 1 * 1024 * 1024 
    
    for chunk_name, file_paths in logical_chunks.items():
        combined_content = ""
        for path in set(file_paths):
            try:
                if os.path.getsize(path) > MAX_FILE_SIZE:
                    st.toast(f"⚠️ Skipping {os.path.basename(path)}: File exceeds 1MB memory limit.")
                    continue
                    
                with open(path, 'r', encoding='utf-8') as f:
                    raw_content = f.read()
                    clean_content = META_BLOCK_REGEX.sub('', raw_content)
                    combined_content += f"\n\n--- FILE: {os.path.basename(path)} ---\n" + clean_content
            except Exception as e:
                st.toast(f"⚠️ Error reading file {path}: {e}")
        
        if combined_content.strip():
            tasks.append(asyncio.create_task(analyze_chunk_async(model, chunk_name, combined_content, semaphore)))
    
    results = await asyncio.gather(*tasks)
    
    all_suggestions = []
    for res in results:
        if isinstance(res, list):
            all_suggestions.extend(res)
            
    unique_suggestions = {item['text']: item for item in all_suggestions if 'text' in item and len(item['text']) > 10}.values()
    return list(unique_suggestions)

# --- 4. DATA ENRICHMENT (COUNTS & FILES) ---
def enrich_suggestions_with_counts(base_path, suggestions):
    for item in suggestions:
        item['occurrences'] = 0
        item['files_found'] = []
        
    for root, _, files in os.walk(base_path):
        for file in files:
            if file.endswith('.rst') and file != "substitutions.rst":
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        raw_content = f.read()
                        clean_content = META_BLOCK_REGEX.sub('', raw_content)
                        
                    for item in suggestions:
                        count = clean_content.count(item['text'])
                        if count > 0:
                            item['occurrences'] += count
                            rel_path = os.path.relpath(file_path, base_path).replace("\\", "/")
                            if rel_path not in item['files_found']:
                                item['files_found'].append(rel_path)
                except Exception as e:
                    st.toast(f"⚠️ Error reading {file_path} for counts: {e}")
                    
    enriched_suggestions = []
    for item in suggestions:
        item['files_found'] = ", ".join(item['files_found'])
        if item['occurrences'] > 1:
            enriched_suggestions.append(item)
            
    enriched_suggestions = sorted(enriched_suggestions, key=lambda x: x['occurrences'], reverse=True)
    return enriched_suggestions

# --- 5. SAFE REGEX WRITER & INJECTOR ---
def get_insertion_index(content):
    header_end = 0
    m_over = re.search(r'^([=~\-\*\+\^\#]{3,})\n[^\n]+\n\1\n', content, re.MULTILINE)
    m_under = re.search(r'^([^\n]+)\n([=~\-\*\+\^\#]{3,})\n', content, re.MULTILINE)
    
    if m_over and m_under:
        header_end = min(m_over.end(), m_under.end())
    elif m_over:
        header_end = m_over.end()
    elif m_under:
        header_end = m_under.end()
        
    m_meta = META_BLOCK_REGEX.search(content)
    meta_end = 0
    if m_meta and m_meta.start() < max(header_end, 500):
        meta_end = m_meta.end()
        
    base_index = max(header_end, meta_end)
    
    tail = content[base_index:]
    current_tail_index = 0
    
    while True:
        m_inc = re.match(r'([ \t]*\n)*[ \t]*\.\. include::[^\n]*(?:\n|$)', tail[current_tail_index:])
        if m_inc:
            current_tail_index += m_inc.end()
        else:
            break
            
    return base_index + current_tail_index

def apply_substitutions_safely(repo_root, base_path, approved_items):
    sub_file_path = os.path.join(base_path, "substitutions.rst")
    
    with open(sub_file_path, 'a', encoding='utf-8') as f:
        f.write("\n\n.. Auto-generated AI Substitutions\n")
        for item in approved_items:
            # Flatten the text: replace newlines with spaces and collapse extra whitespace
            flat_text = re.sub(r'\s+', ' ', item['text']).strip()
            f.write(f".. {item['tag']} replace:: {flat_text}\n")

    rel_sub_path = os.path.relpath(sub_file_path, repo_root).replace("\\", "/")
    include_statement = f".. include:: /{rel_sub_path}"

    for root, _, files in os.walk(base_path):
        for file in files:
            if file.endswith('.rst') and file != "substitutions.rst":
                file_path = os.path.join(root, file)
                with open(file_path, 'r', encoding='utf-8') as f:
                    original_content = f.read()
                
                parts = META_BLOCK_REGEX.split(original_content)
                new_parts = []
                
                for i, part in enumerate(parts):
                    if i % 2 == 0:
                        for item in approved_items:
                            escaped_old_text = re.escape(item['text'])
                            part = re.sub(escaped_old_text, item['tag'], part)
                    new_parts.append(part)
                
                content = "".join(new_parts)
                
                if content != original_content:
                    if include_statement not in content:
                        insert_pos = get_insertion_index(content)
                        before = content[:insert_pos].rstrip()
                        after = content[insert_pos:].lstrip()
                        content = f"{before}\n\n{include_statement}\n\n{after}\n"
                        
                    # Strip trailing whitespace from every line to prevent doc-lint failures
                    clean_lines = [line.rstrip() for line in content.splitlines()]
                    content = "\n".join(clean_lines) + "\n"
                        
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)

# --- 6. MAIN UI WORKFLOW ---
def main():
    # --- UI CREDENTIAL INJECTION & WORKSPACE CLEANUP ---
    with st.sidebar:
        st.header("🔑 Credentials Setup")
        st.markdown("Enter your personal credentials to use the app.")
        
        github_pat = st.text_input("GitHub PAT", type="password", help="Requires 'repo' scope to push and create PRs.")
        gemini_key = st.text_input("Gemini API Key", type="password", help="Your personal Google AI Studio key.")
        
        # Secure Workspace Cleanup Button
        if st.button("🚪 Logout & Clean Workspace", type="primary"):
            if github_pat:
                user_hash = hashlib.md5(github_pat.encode()).hexdigest()[:8]
                repo_to_clean = os.path.join(tempfile.gettempdir(), f"docs_repo_{user_hash}")
                if os.path.exists(repo_to_clean):
                    shutil.rmtree(repo_to_clean, ignore_errors=True)
            st.session_state.clear()
            st.rerun()

    if not github_pat or not gemini_key:
        st.warning("👈 Please enter your GitHub PAT and Gemini API Key in the sidebar to continue.")
        st.stop()
        
    genai.configure(api_key=gemini_key)
    
    user_hash = hashlib.md5(github_pat.encode()).hexdigest()[:8]
    REPO_DIR = os.path.join(tempfile.gettempdir(), f"docs_repo_{user_hash}")

    st.write("### 0. Repository Setup")
    
    if st.button("⬇️ Clone / Pull Latest Docs Repository"):
        with st.spinner("Fetching repository data... this might take a moment."):
            try:
                auth_url = f"https://oauth2:{github_pat}@{REPO_URL}"
                
                if not os.path.exists(os.path.join(REPO_DIR, ".git")):
                    git.Repo.clone_from(auth_url, REPO_DIR)
                    st.success("Repository cloned successfully!")
                else:
                    repo = git.Repo(REPO_DIR)
                    repo.remotes.origin.set_url(auth_url)
                    repo.remotes.origin.pull()
                    st.success("Repository pulled and is up to date!")
                    
                st.session_state['repo_ready'] = True
            except Exception as e:
                st.error(f"Failed to fetch repository. Check your PAT and ensure you have access to `{REPO_URL}`. Error: {e}")

    if st.session_state.get('repo_ready', False) or os.path.exists(os.path.join(REPO_DIR, ".git")):
        st.divider()
        repo = git.Repo(REPO_DIR)

        all_subdirs = []
        for root, dirs, files in os.walk(REPO_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            if root != REPO_DIR:
                rel_path = os.path.relpath(root, REPO_DIR).replace("\\", "/")
                all_subdirs.append(rel_path)
                
        all_subdirs.sort() 
        selected_folder = st.selectbox("Select Project Folder to Analyze", ["/ (Root)"] + all_subdirs)

        target_path = REPO_DIR if selected_folder == "/ (Root)" else os.path.join(REPO_DIR, selected_folder)

        if st.button("1. Analyze .rst Files Concurrently"):
            with st.spinner(f"Mapping folders in '{selected_folder}' and analyzing with Gemini..."):
                try:
                    logical_chunks = get_logical_chunks_recursive(target_path, "index.rst")
                    if not logical_chunks:
                        st.warning("No index.rst found or no chunks generated. Falling back to simple file read.")
                        logical_chunks = {"Fallback_Chunk": [os.path.join(root, f) for root, _, files in os.walk(target_path) for f in files if f.endswith('.rst')]}
                    else:
                        st.info(f"Successfully mapped {len(logical_chunks)} logical sections from index.rst files.")
                        
                    raw_suggestions = asyncio.run(process_all_chunks_concurrently(logical_chunks))
                    enriched_suggestions = enrich_suggestions_with_counts(target_path, raw_suggestions)
                    
                    st.session_state['suggestions'] = enriched_suggestions
                    st.success(f"Found {len(enriched_suggestions)} high-value substitution candidates!")
                except Exception as e:
                    st.error(f"Analysis failed: {e}")

        if 'suggestions' in st.session_state and st.session_state['suggestions']:
            st.write("### 2. Review Suggested Substitutions")
            st.info("Check the 'approved' box for the tags you want to keep. Feel free to edit the tag names.")
            
            edited_df = st.data_editor(st.session_state['suggestions'], num_rows="dynamic", use_container_width=True)
            
            st.write("### 3. Version Control & Pull Request")
            
            try:
                remote_refs = repo.remote().refs
                available_branches = list(set([ref.name.replace('origin/', '') for ref in remote_refs if ref.name != 'origin/HEAD']))
                
                if 'main' in available_branches:
                    available_branches.insert(0, available_branches.pop(available_branches.index('main')))
                elif 'master' in available_branches:
                    available_branches.insert(0, available_branches.pop(available_branches.index('master')))
            except Exception:
                available_branches = ["main", "master"]

            col1, col2 = st.columns(2)
            
            with col1:
                raw_branch_name = st.text_input("New Branch Name (Head)", value="feature/ai-docs-update")
                safe_branch_name = re.sub(r'[^a-zA-Z0-9.\-_/]', '', raw_branch_name.replace(" ", "-"))
                if safe_branch_name != raw_branch_name:
                    st.caption(f"ℹ️ *Sanitized to:* `{safe_branch_name}`")

            with col2:
                base_branch = st.selectbox("Target Branch (Base)", options=available_branches)

            if st.button("Apply Approved Substitutions & Create PR"):
                approved_items = [item for item in edited_df if item.get('approved') == True]
                
                if not approved_items:
                    st.warning("No substitutions approved. Please check at least one box.")
                elif not safe_branch_name:
                     st.warning("Please provide a valid branch name.")
                else:
                    with st.spinner(f"Applying changes and creating PR from `{safe_branch_name}` into `{base_branch}`..."):
                        try:
                            apply_substitutions_safely(REPO_DIR, target_path, approved_items)
                            
                            branch_name = safe_branch_name
                            if branch_name not in [b.name for b in repo.branches]:
                                repo.git.checkout('-b', branch_name)
                            else:
                                repo.git.checkout(branch_name)

                            repo.git.add(A=True)
                            repo.index.commit("docs: apply AI suggested reST substitutions")
                            
                            origin = repo.remote(name='origin')
                            auth_url = f"https://oauth2:{github_pat}@{REPO_URL}"
                            repo.remotes.origin.set_url(auth_url)
                            origin.push(refspec=f'{branch_name}:{branch_name}')
                            
                            gh_repo_path = REPO_URL.replace("github.com/", "").replace(".git", "")
                            g = Github(github_pat)
                            gh_repo = g.get_repo(gh_repo_path)
                            
                            pr_title = f"Docs: AI Suggested reST Substitutions from '{branch_name}'"
                            pr_body = (
                                "This PR was automatically generated by the Alation Substitution Builder app.\n\n"
                                f"**Target Branch:** `{base_branch}`\n"
                                f"**Substitutions Applied:** {len(approved_items)}\n\n"
                                "Please review the changes to ensure the AI-suggested Regex replacements did not disrupt formatting."
                            )
                            
                            pr = gh_repo.create_pull(
                                title=pr_title, 
                                body=pr_body, 
                                head=branch_name, 
                                base=base_branch 
                            )
                            
                            st.success(f"🎉 Successfully applied substitutions, pushed branch, and created PR!")
                            st.markdown(f"**👉 [Click here to review your Pull Request]({pr.html_url})**")
                            
                            del st.session_state['suggestions']
                            
                        except Exception as e:
                            st.error(f"Git push or PR creation failed. Note: If no actual text was changed, Git will reject the push. Error: {e}")

if __name__ == "__main__":
    main()
