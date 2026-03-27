# 🛠️ Alation Substitution Builder

A Streamlit-based web application designed to automate the optimization of Sphinx-based reStructuredText (.rst) documentation. 

This tool clones your documentation repository, logically maps the structure using `index.rst` `toctree` directives, and leverages **Google's Gemini 2.5 Pro** AI to identify repetitive inline phrases and UI navigation steps. It then allows you to review these suggestions, safely applies the `.. |tag| replace::` substitutions across your files, and automatically creates a GitHub Pull Request with the changes.

## ✨ Features

* **Smart Repository Handling:** Clones and pulls your target GitHub repository into a temporary local workspace using a Personal Access Token (PAT).
* **Logical Sphinx Parsing:** Recursively maps your documentation structure by following `index.rst` files, ensuring the AI analyzes logical chunks of documentation rather than isolated files.
* **Asynchronous AI Analysis:** Uses `gemini-2.5-pro` concurrently to quickly scan large documentation sets and extract high-value substitution candidates.
* **Data Enrichment:** Automatically counts occurrences and tracks exactly which `.rst` files contain the suggested repetitive text.
* **Safe reST Injection:** * Safely ignores Sphinx meta blocks to prevent breaking document metadata.
  * Automatically calculates the safest insertion point for `.. include:: /substitutions.rst`.
  * Generates the `substitutions.rst` file and performs regex-safe text replacements.
* **Automated Pull Requests:** Branches your repository, commits the approved substitutions, pushes to GitHub, and generates a Pull Request via the GitHub API.

## 📋 Prerequisites

* **Python:** 3.8+
* **Git:** Installed and available in your system's PATH.
* **GitHub PAT:** A Personal Access Token with `repo` scope (for cloning, pushing, and creating PRs).
* **Gemini API Key:** A personal API key from Google AI Studio.

## 🚀 Installation & Setup

1. **Clone this application repository:**

   ```bash
   git clone <your-app-repo-url>
   cd <your-app-directory>
   ```
3. **Install Python dependencies:**
   It is highly recommended to use a Python virtual environment.

   ```bash
   pip install streamlit GitPython PyGithub google-generativeai
   ```
4. **Configure Secrets:**
   The app requires a base Streamlit secret to know which repository to target. Create a `.streamlit` folder in the root directory and add a `secrets.toml` file.
   `.streamlit/secrets.toml`

   ```ini,toml
   # The target documentation repository URL (no https:// prefix)
   REPO_URL = "[github.com/your-org/your-docs-repo.git](https://github.com/your-org/your-docs-repo.git)"
   ```

## 💻 Usage

1. Run the app locally:

  ```bash
  streamlit run app.py
  ```
2. **Authenticate:** Enter your GitHub PAT and Gemini API Key in the secure sidebar.

3. **Fetch Repository:** Click the button to clone or pull the latest version of your target repository.

4. **Analyze:** Select a specific project folder (or the root) and click **1. Analyze .rst Files Concurrently**.

5. **Review:** An interactive table will appear. Check the approved box for any substitution you want to apply, and optionally edit the AI-suggested tag names.

6. **Create PR:** Provide a new branch name, select your target base branch, and click **Apply Approved Substitutions & Create PR**. The app will provide a direct link to your new GitHub Pull Request!

## 🧹 Security & Cleanup

Because this app handles source code and credentials:

  - Credentials entered in the UI are kept strictly in Streamlit's session state and are not saved to disk.

  - A Logout & Clean Workspace button is provided in the sidebar. This immediately wipes the temporary cloned repository from your local machine and clears your session state.
