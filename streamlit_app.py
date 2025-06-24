import streamlit as st
import os
from dotenv import load_dotenv
from openai import OpenAI
import json
from datetime import datetime
import logging
import re
import io # For handling PDF data in memory
import httpx # Re-import httpx
import zipfile # Import zipfile
import urllib3 # For suppressing SSL warnings
import requests # For OFAC search
from bs4 import BeautifulSoup # For parsing HTML results
import urllib.parse # For URL encoding
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml
from docx.oxml.shared import qn

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ReportLab imports removed - PDF generation no longer needed

# --- Initialize Session State (add this near the top) ---
if 'results_list' not in st.session_state:
    st.session_state.results_list = [] # Initialize if not already present

# Initialize authentication state
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False

# --- Page Config (MUST be the first Streamlit command) ---
st.set_page_config(
    page_title="Axos Internal AML Demo", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Version number
APP_VERSION = "1.71"

# Configure logging with enhanced format
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('aml_app.log', mode='a')
    ]
)

# Function to get client IP address
def get_client_ip():
    """Get the client's IP address from Streamlit context"""
    try:
        # Try to get from modern Streamlit context headers
        try:
            import streamlit as st
            headers = st.context.headers
            if headers:
                # Check for forwarded headers first (for proxy/load balancer scenarios)
                forwarded_for = headers.get('x-forwarded-for') or headers.get('x-real-ip')
                if forwarded_for:
                    return forwarded_for.split(',')[0].strip()
        except (AttributeError, ImportError):
            # Fallback for older Streamlit versions (but this will show deprecation warning)
            try:
                from streamlit.web.server.websocket_headers import _get_websocket_headers
                headers = _get_websocket_headers()
                if headers:
                    # Check for forwarded headers first (for proxy/load balancer scenarios)
                    forwarded_for = headers.get('x-forwarded-for', headers.get('x-real-ip'))
                    if forwarded_for:
                        return forwarded_for.split(',')[0].strip()
            except ImportError:
                pass
        
        # Fallback: try to get from Streamlit runtime
        import streamlit.runtime.scriptrunner as sr
        session_info = sr.get_script_run_ctx()
        if session_info and hasattr(session_info, 'session_id'):
            return f"session_{session_info.session_id[:8]}"
            
    except Exception as e:
        logging.debug(f"Could not determine client IP: {e}")
    
    return "unknown_ip"

# Function to log user requests
def log_user_request(request_type, company_name, model=None, additional_info=None):
    """Log user requests with IP address and details"""
    client_ip = get_client_ip()
    log_message = f"USER_REQUEST - IP: {client_ip} - Type: {request_type} - Company: '{company_name}'"
    
    if model:
        log_message += f" - Model: {model}"
    
    if additional_info:
        log_message += f" - Info: {additional_info}"
    
    logging.info(log_message)

# --- Configuration & API Client Setup ---
load_dotenv() # Load .env file for local development

PERPLEXITY_API_KEY = None
SOURCE_MESSAGE = "Key Source: Not found yet."
API_KEY_LOADED_SUCCESSFULLY = False

# 1. Try environment variable
_raw_key_env = os.getenv('PERPLEXITY_API_KEY')
if _raw_key_env:
    PERPLEXITY_API_KEY = _raw_key_env.strip()
    if PERPLEXITY_API_KEY: # Check if non-empty after strip
        SOURCE_MESSAGE = "Key Source: Environment Variable"
        API_KEY_LOADED_SUCCESSFULLY = True
        logging.info(f"{SOURCE_MESSAGE}")
    else:
        logging.warning("Found PERPLEXITY_API_KEY in env vars, but it was empty.")

# 2. If not found in env, try st.secrets
if not API_KEY_LOADED_SUCCESSFULLY:
    logging.info("API key not found in env vars, trying st.secrets...")
    SOURCE_MESSAGE = "Key Source: Streamlit Secrets"
    try:
        _raw_key_secrets = st.secrets.get("PERPLEXITY_API_KEY") # Use .get for safety
        if _raw_key_secrets:
            PERPLEXITY_API_KEY = _raw_key_secrets.strip()
            if PERPLEXITY_API_KEY:
                logging.info("Loaded API key from st.secrets")
                API_KEY_LOADED_SUCCESSFULLY = True
                # SOURCE_MESSAGE already set
            else:
                logging.warning("Found PERPLEXITY_API_KEY in st.secrets, but it was empty after stripping.")
                SOURCE_MESSAGE = "Key Source: Streamlit Secrets (Empty Key!)"
        else:
             logging.warning("PERPLEXITY_API_KEY not found in st.secrets.")
             SOURCE_MESSAGE = "Key Source: Streamlit Secrets (Not Found)"
             
    except FileNotFoundError:
        logging.info("secrets.toml file not found (expected locally). Skipping st.secrets.")
        SOURCE_MESSAGE = "Key Source: Streamlit Secrets (File Not Found)"
    except Exception as e:
        logging.error(f"An unexpected error occurred while accessing st.secrets: {e}")
        SOURCE_MESSAGE = f"Key Source: Streamlit Secrets (Error: {e})"

# --- Add Debugging Output Early --- 
st.sidebar.info(SOURCE_MESSAGE) # Show where the key was (or wasn't) found
if API_KEY_LOADED_SUCCESSFULLY:
    # Mask key for display
    masked_key = f"{PERPLEXITY_API_KEY[:7]}...{PERPLEXITY_API_KEY[-4:]}" if PERPLEXITY_API_KEY and len(PERPLEXITY_API_KEY) > 11 else "Invalid Key Format"
    st.sidebar.success(f"API Key Status: Loaded ({masked_key})")
else:
    st.sidebar.error("API Key Status: NOT loaded.")
# --- End Debugging Output ---

PERPLEXITY_API_BASE_URL = "https://api.perplexity.ai"

openai_client = None
client_init_error_msg = None

if PERPLEXITY_API_KEY:
    # Log the key just before use (masked)
    masked_key_for_log = f"{PERPLEXITY_API_KEY[:7]}...{PERPLEXITY_API_KEY[-4:]}" if PERPLEXITY_API_KEY and len(PERPLEXITY_API_KEY) > 11 else "Invalid Key Format"
    logging.info(f"Attempting to initialize OpenAI client with key: {masked_key_for_log}")
    try:
        # RE-ADD: Explicitly create an httpx client that ignores system proxies
        http_client = httpx.Client(verify=False)  # Disable SSL verification
        
        # RE-ADD: Pass the custom http_client
        openai_client = OpenAI(
            api_key=PERPLEXITY_API_KEY, 
            base_url=PERPLEXITY_API_BASE_URL,
            http_client=http_client 
        )
        logging.info("OpenAI client initialized pointing to Perplexity API.")
        st.sidebar.success("API Client Status: Initialized.")
    except Exception as client_init_error:
        client_init_error_msg = str(client_init_error) 
        logging.error(f"Failed to initialize OpenAI client: {client_init_error_msg}", exc_info=True)
        openai_client = None 
        st.sidebar.error(f"API Client Status: Failed ({client_init_error_msg})")
else:
    st.sidebar.warning("API Client Status: Not initialized (No API Key).")

# Error message shown only if client is STILL None
if not openai_client:
    # Construct error message without accessing sidebar elements
    final_error_msg = "ERROR: Perplexity API client could not be initialized. "
    if not API_KEY_LOADED_SUCCESSFULLY:
        final_error_msg += f"API key was not loaded (checked {SOURCE_MESSAGE}). "
    elif client_init_error_msg:
        final_error_msg += f"Client initialization failed: {client_init_error_msg}. "
    else:
         final_error_msg += "Unknown initialization error. " # Fallback
    final_error_msg += "Please check API Key, app logs, and verify configuration."
    
    st.error(final_error_msg)
    st.stop()

# --- Helper function for Recommendation Color ---
def get_recommendation_color(recommendation):
    if recommendation == 'PROCEED': return "green"
    if recommendation == 'ENHANCED DUE DILIGENCE': return "orange"
    if recommendation == 'HIGH RISK': return "#FF4500" # orangered
    if recommendation == 'DO NOT PROCEED': return "red"
    return "grey"
# --- End Helper Function ---

# PDF formatting functions removed - no longer needed

# Markdown helper functions removed - no longer needed for PDF generation

# --- Constants ---
NEGATIVE_KEYWORDS = '(arrest OR bankruptcy OR BSA OR conviction OR criminal OR fraud OR trafficking OR lawsuit OR "money laundering" OR OFAC OR Ponzi OR terrorist OR violation OR "honorary consul" OR consul OR "Panama Papers" OR theft OR corruption OR bribery)'

# Preferred domain list for enhanced searches
PREFERRED_DOMAINS = [
    "sec.gov",
    "finra.org", 
    "treasury.gov",
    "oig.gov",
    "cftc.gov",
    "fdic.gov",
    "federalreserve.gov",
    "justice.gov",
    "fbi.gov",
    "fincen.gov",
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "law360.com"
]

# Perplexity Models - Available for testing
PERPLEXITY_MODELS = {
    "sonar-pro": {
        "name": "Sonar Pro",
        "description": "Fast, efficient AI search with real-time web access",
        "use_case": "Standard research and analysis",
        "max_tokens": 8000
    },
    "sonar-deep-research": {
        "name": "Sonar Deep Research", 
        "description": "Exhaustive research across hundreds of sources with expert-level analysis",
        "use_case": "Comprehensive reports and detailed investigations",
        "max_tokens": 2000
    }
}

# Default models for different use cases
DEFAULT_PERPLEXITY_MODEL = "sonar-pro"
DEFAULT_COMPREHENSIVE_MODEL = "sonar-pro"
DEFAULT_OFAC_FALLBACK_MODEL = "sonar-pro"

# Axos Bank Logo (SVG as base64)
AXOS_LOGO_SVG = """
<svg width="200" height="60" viewBox="0 0 200 60" xmlns="http://www.w3.org/2000/svg">
  <!-- Axos logo recreation -->
  <defs>
    <style>
      .axos-text { font-family: Arial, sans-serif; font-weight: bold; }
      .axos-blue { fill: #2c4f7c; }
      .axos-orange { fill: #f39c12; }
    </style>
  </defs>
  
  <!-- Letter 'a' -->
  <path class="axos-blue" d="M10 45 Q10 15 25 15 Q40 15 40 30 Q40 45 25 45 L15 45 L15 35 L25 35 Q30 35 30 30 Q30 25 25 25 Q20 25 20 30 L20 45 Z"/>
  
  <!-- Letter 'x' with orange accent -->
  <path class="axos-orange" d="M50 15 L65 30 L80 15 L85 20 L70 35 L85 50 L80 55 L65 40 L50 55 L45 50 L60 35 L45 20 Z"/>
  
  <!-- Letter 'o' -->
  <circle class="axos-blue" cx="100" cy="30" r="15" fill="none" stroke="#2c4f7c" stroke-width="8"/>
  
  <!-- Letter 's' -->
  <path class="axos-blue" d="M125 25 Q125 15 135 15 Q145 15 145 25 Q145 30 135 30 Q125 30 125 35 Q125 45 135 45 Q145 45 145 35 L155 35 Q155 55 135 55 Q115 55 115 35 Q115 25 135 25 Q145 25 145 20 Q145 15 135 15 Q125 15 125 25 Z"/>
  
  <!-- "BANK" text -->
  <text x="10" y="58" class="axos-text axos-blue" font-size="8" letter-spacing="3">B A N K</text>
</svg>
"""

# --- Authentication System ---
# Password configuration
APP_PASSWORD = "AML2024secure!"  # Change this to your desired password

def check_password():
    """Returns True if the user has entered the correct password."""
    
    def password_entered():
        """Checks whether a password entered by the user is correct."""
        try:
            client_ip = get_client_ip()
        except NameError:
            client_ip = "unknown_ip"
        
        if st.session_state["password"] == APP_PASSWORD:
            st.session_state["authenticated"] = True
            logging.info(f"AUTH_SUCCESS - IP: {client_ip} - User successfully authenticated")
            del st.session_state["password"]  # Don't store password
        else:
            st.session_state["authenticated"] = False
            logging.warning(f"AUTH_FAILED - IP: {client_ip} - Failed authentication attempt")

    if not st.session_state.get("authenticated", False):
        # Show login form
        st.markdown("# 🔐 AML Research Platform")
        st.markdown("### Please enter the access password to continue")
        
        st.text_input(
            "Password", 
            type="password", 
            on_change=password_entered, 
            key="password",
            placeholder="Enter password to access the system"
        )
        
        if "password" in st.session_state and st.session_state["password"]:
            if not st.session_state.get("authenticated", False):
                st.error("❌ Incorrect password. Please try again.")
        
        st.info("💡 This system is restricted to authorized personnel only.")
        return False
    else:
        return True

# Check authentication before proceeding
if not check_password():
    st.stop()

# Add logout button to sidebar for authenticated users
with st.sidebar:
    st.markdown("---")
    if st.button("🚪 Logout", type="secondary"):
        st.session_state.authenticated = False
        logging.info(f"AUTH_LOGOUT - IP: {get_client_ip()} - User logged out")
        st.rerun()

# --- Core Functions (Adapted from Flask app) ---

def search_with_perplexity(company_name, model=DEFAULT_PERPLEXITY_MODEL, reasoning_effort="medium"):
    # (This function remains largely the same as in app.py)
    # ... (API call logic, prompt, message structure) ...
    client_ip = get_client_ip()
    model_config = PERPLEXITY_MODELS.get(model, {})
    logging.info(f"SEARCH_START - IP: {client_ip} - Perplexity search for '{company_name}' using model '{model}' ({model_config.get('name', 'Unknown Model')})")
    if not openai_client:
        logging.error("OpenAI client (for Perplexity) not initialized.")
        return {"status": "failed", "error": "Perplexity API client not initialized.", "answer": None, "citations": [], "aml_grade": None}
    try:
        # Build domain instruction using preferred domains
        domain_instruction = f"\n\nIMPORTANT: Prioritize information from these authoritative domains first: {', '.join(PREFERRED_DOMAINS)}. Search these regulatory, financial, and news sources before other sources as they provide the most reliable AML-relevant information."
        
        # Updated Prompt: Ask for explicit separation with headings
        prompt = (
            f"Provide a comprehensive AML (Anti-Money Laundering) due diligence assessment for '{company_name}'. {domain_instruction}"
            f"\n\nStructure your response as follows:"
            f"\n\n## Subject Summary"
            f"\nProvide a brief summary of the subject '{company_name}', including business activities, key executives, and geographic presence.\n"
            f"\n\n## AML Risk Assessment"
            f"\nAnalyze any negative news found regarding this subject, focusing on: {NEGATIVE_KEYWORDS}. "
            f"Organize findings into clear categories such as 'Financial Crimes', 'Regulatory Issues', 'Legal Proceedings', etc. "
            f"For each finding, include when it happened, key parties involved, and current status if available. "
            f"If no relevant negative news is found in a category, state that clearly.\n"
            f"\n\n## Summary & Overall Assessment"
            f"\nProvide a clear summary of key findings identified regarding this entity. "
            f"Summarize the overall situation including:"
            f"\n- Key risk factors identified (if any)"
            f"\n- Notable compliance or regulatory issues"
            f"\n- Current status of any ongoing matters"
            f"\n- Overall risk profile based on available information"
            f"\n\nPresent findings objectively without making business recommendations. "
            f"Use double line breaks between sections. Provide citations as numeric references like [1], [2] etc., within the text where applicable."
        )
        # For Sonar models, system prompts are ignored - combine everything in user message
        if "sonar" in model.lower():
            full_prompt = f"As an expert AML analyst performing subject due diligence, provide comprehensive analysis with clear subject summary, risk assessment with organized categories, and objective findings summary. Use numeric citations [1] and maintain clean formatting with proper section headers.\n\n{prompt}"
            messages = [
                {"role": "user", "content": full_prompt}
            ]
        else:
            # For other models, use system + user structure
            messages = [
                {
                    "role": "system",
                    "content": "You are an expert AML analyst performing subject due diligence. Provide comprehensive analysis with clear subject summary, risk assessment with organized categories, and objective findings summary. Use numeric citations [1] and maintain clean formatting with proper section headers.",
                },
                {"role": "user", "content": prompt},
            ]
        
        # Build API parameters based on model type
        max_tokens = PERPLEXITY_MODELS.get(model, {}).get("max_tokens", 2000)
        api_params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens
        }
        
        # Note: reasoning_effort parameter not yet supported in current OpenAI client version
        # Will be added when the client is updated to support this parameter
        if "deep-research" in model.lower():
            logging.info(f"Deep research model selected - enhanced analysis will be performed")
        
        # Only add temperature for non-Sonar models (real-time search models don't use temperature)
        if "sonar" not in model.lower():
            api_params["temperature"] = 0.1
        
        # Note: web_search_options removed as it's not supported by the current API
        
        logging.info(f"Calling Perplexity API with model: {model} (max_tokens: {max_tokens})...")
        response = openai_client.chat.completions.create(**api_params)
        logging.info(f"Perplexity API call completed using model: {model}")
        
        full_answer_content = None
        citations = []
        recommendation = None
        if response.choices and len(response.choices) > 0:
            message = response.choices[0].message
            if message and message.content:
                full_answer_content = message.content
                # No longer extracting recommendations - providing objective findings only
                recommendation = None
            # --- Citation Extraction (same as before) ---
            raw_citations = []
            # ... (check message.citations, response.citations) ...
            if hasattr(message, 'citations') and message.citations:
                 raw_citations = message.citations
            elif hasattr(response, 'citations') and response.citations:
                 raw_citations = response.citations
                 
            if raw_citations:
                 for cit in raw_citations:
                     # ... (standardize to dict) ...
                     citation_dict = {'url': '#', 'title': 'Source'}
                     if isinstance(cit, dict):
                         citation_dict['url'] = cit.get('url', '#')
                         citation_dict['title'] = cit.get('title', cit.get('url', 'Source'))
                     elif hasattr(cit, 'url'):
                          citation_dict['url'] = getattr(cit, 'url', '#')
                          citation_dict['title'] = getattr(cit, 'title', getattr(cit, 'url', 'Source'))
                     elif isinstance(cit, str):
                          citation_dict['url'] = cit
                          citation_dict['title'] = cit
                     else:
                          citation_dict['title'] = str(cit)
                     citations.append(citation_dict)
                     
        if not full_answer_content:
            full_answer_content = "No summary could be generated by Perplexity."
            
        return {"status": "success", "error": None, "answer": full_answer_content, "citations": citations, "recommendation": recommendation}

    except Exception as e:
        logging.error(f"Error during Perplexity search for {company_name}: {str(e)}", exc_info=True)
        return {"status": "failed", "error": str(e), "answer": None, "citations": [], "recommendation": None}

def search_with_ofac(query):
    """Search OFAC sanctions database"""
    client_ip = get_client_ip()
    logging.info(f"SEARCH_START - IP: {client_ip} - OFAC search for '{query}'")
    try:
        session = requests.Session()
        session.verify = False
        
        # Get the initial page to extract form data
        initial_url = "https://sanctionssearch.ofac.treas.gov/"
        initial_response = session.get(initial_url, timeout=30)
        
        if initial_response.status_code != 200:
            return f"Failed to access OFAC website. Status code: {initial_response.status_code}"
        
        soup = BeautifulSoup(initial_response.content, 'html.parser')
        
        # Extract form data
        form_data = {}
        
        # Get all hidden fields
        hidden_fields = soup.find_all('input', {'type': 'hidden'})
        for field in hidden_fields:
            name = field.get('name')
            value = field.get('value', '')
            if name:
                form_data[name] = value
        
        # Add search parameters
        form_data.update({
            'ctl00$MainContent$txtLastName': query,
            'ctl00$MainContent$ddlType': '',
            'ctl00$MainContent$txtAddress': '',
            'ctl00$MainContent$txtCity': '',
            'ctl00$MainContent$txtID': '',
            'ctl00$MainContent$txtState': '',
            'ctl00$MainContent$lstPrograms': '',
            'ctl00$MainContent$ddlCountry': '',
            'ctl00$MainContent$ddlList': '',
            'ctl00$MainContent$Slider1': '83',
            'ctl00$MainContent$Slider1_Boundcontrol': '83',
            'ctl00$MainContent$btnSearch': 'Search',
            '__EVENTTARGET': 'ctl00$MainContent$btnSearch',
            '__EVENTARGUMENT': ''
        })
        
        # Remove the button value since we're using __EVENTTARGET
        if 'ctl00$MainContent$btnSearch' in form_data:
            del form_data['ctl00$MainContent$btnSearch']
        
        # Submit the search
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': initial_url,
            'Origin': 'https://sanctionssearch.ofac.treas.gov'
        }
        
        search_response = session.post(initial_url, data=form_data, headers=headers, timeout=30)
        
        if search_response.status_code != 200:
            return f"Search request failed. Status code: {search_response.status_code}"
        
        # Parse results
        result_soup = BeautifulSoup(search_response.content, 'html.parser')
        
        # Look for results table
        results_table = result_soup.find('table', {'id': 'gvSearchResults'})
        
        if not results_table:
            # Check for "no results" indicators
            results_div = result_soup.find('div', {'id': 'ctl00_MainContent_divResults'})
            if results_div:
                results_text = results_div.get_text()
                if "0 Found" in results_text or "No results" in results_text.lower():
                    return "✅ No matches found in OFAC sanctions database - entity appears clean"
            
            return "❌ Could not parse search results from OFAC website"
        
        # Parse the results table
        rows = results_table.find_all('tr')
        if len(rows) <= 1:  # Only header row or no rows
            return "✅ No matches found in OFAC sanctions database - entity appears clean"
        
        # Extract results count from the page
        results_count = 0
        results_label = result_soup.find('span', {'id': 'ctl00_MainContent_lblResults'})
        if results_label:
            results_text = results_label.get_text()
            import re
            count_match = re.search(r'(\d+)\s+Found', results_text)
            if count_match:
                results_count = int(count_match.group(1))
        
        if results_count == 0:
            return "✅ **OFAC CLEAR**: No matches found in OFAC sanctions database\n\n**Summary**: Entity appears clean from sanctions perspective.\n\n**Recommendation**: Proceed with standard due diligence protocols."
        
        # Initialize variables at the start
        all_matches = []
        high_confidence_matches = 0
        
        # Process ALL rows - OFAC table has no header row to skip
        for i, row in enumerate(rows, 1):
            cells = row.find_all('td')
            
            # Must have at least 6 cells for a valid data row
            if len(cells) < 6:
                continue
                
            try:
                # Extract all cell data
                name_cell = cells[0]
                name_link = name_cell.find('a')
                name = name_link.get_text().strip() if name_link else name_cell.get_text().strip()
                
                address = cells[1].get_text().strip()
                entity_type = cells[2].get_text().strip()
                programs = cells[3].get_text().strip()
                list_type = cells[4].get_text().strip()
                score = cells[5].get_text().strip()
                
                # Parse score - handle both "100%" and "100" formats
                score_num = 0
                if score:
                    try:
                        score_clean = score.replace('%', '').strip()
                        score_num = float(score_clean)
                    except:
                        continue
                else:
                    continue
                
                # Apply 83% threshold
                if score_num < 83:
                    continue

                # Track high confidence matches (any match above 83% threshold is significant)
                if score_num >= 83:
                    high_confidence_matches += 1
                
                # Store match info
                score_display = f"{score_num}%" if not score.endswith('%') else score
                all_matches.append({
                    'name': name,
                    'address': address,
                    'entity_type': entity_type,
                    'programs': programs,
                    'list_type': list_type,
                    'score': score_display,
                    'score_num': score_num
                })
                
            except Exception as e:
                continue
        
        # Check if no matches found
        if len(all_matches) == 0:
            return "✅ **OFAC CLEAR**: No matches found in OFAC sanctions database\n\n**Summary**: Entity appears clean from sanctions perspective.\n\n**Recommendation**: Proceed with standard due diligence protocols."
        
        # Sort matches by score (highest first)
        all_matches.sort(key=lambda x: x['score_num'], reverse=True)
        
        # Format the results
        result_text = f"🚨 **SANCTIONS ALERT**: Found {len(all_matches)} result{'s' if len(all_matches) > 1 else ''} in OFAC database\n"
        result_text += f"*(Minimum match threshold: 83%)*\n\n"
        
        # Display all matches
        for i, match in enumerate(all_matches):
            result_text += f"**Match #{i + 1}: {match['name']}**\n"
            result_text += f"• **Match Score**: {match['score']}\n"
            if match['address']:
                result_text += f"• **Address**: {match['address']}\n"
            result_text += f"• **Entity Type**: {match['entity_type']}\n"
            result_text += f"• **Programs**: {match['programs']}\n"
            result_text += f"• **List**: {match['list_type']}\n"
            result_text += f"\n"
        
        # Add summary and recommendation
        result_text += "---\n\n"
        
        # Check for 100% matches specifically
        perfect_matches = [match for match in all_matches if match['score_num'] == 100]
        
        if perfect_matches:
            # Special message for 100% matches
            result_text += f"**Summary**: Subject returned a 100% match - escalate to management immediately.\n\n"
            result_text += "**Recommendation**: 🚨 **ESCALATE TO MANAGEMENT IMMEDIATELY** - Perfect match detected in OFAC sanctions database."
        elif high_confidence_matches > 0:
            result_text += f"**Summary**: {high_confidence_matches} match{'es' if high_confidence_matches > 1 else ''} (83%+) detected. Entity has strong similarity to sanctioned individuals/entities.\n\n"
            result_text += "**Recommendation**: ⛔ **DO NOT PROCEED** - Conduct thorough manual review and legal consultation before any business relationship."
        else:
            result_text += f"**Summary**: No matches found above 83% similarity threshold.\n\n"
            result_text += "**Recommendation**: ✅ **PROCEED** - No significant OFAC sanctions concerns identified."
        
        # Add direct search URL
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        direct_url = f"https://sanctionssearch.ofac.treas.gov/?search={encoded_query}"
        result_text += f"\n\n**Direct OFAC Search URL**: {direct_url}"
        
        return result_text
        
    except requests.exceptions.Timeout:
        return "❌ OFAC search timed out. Please try again."
    except requests.exceptions.RequestException as e:
        return f"❌ Error connecting to OFAC database: {str(e)}"
    except Exception as e:
        return f"❌ Error during OFAC search: {str(e)}"

# PDF generation function removed - no longer needed

def search_with_comprehensive(company_name, model=DEFAULT_COMPREHENSIVE_MODEL):
    """Comprehensive search combining Perplexity AI research with OFAC sanctions screening"""
    client_ip = get_client_ip()
    logging.info(f"SEARCH_START - IP: {client_ip} - Comprehensive search for '{company_name}' using model '{model}'")
    try:
        # First, perform OFAC sanctions search
        logging.info(f"Starting comprehensive search for {company_name} - OFAC phase")
        ofac_result = search_with_ofac(company_name)
        
        # Analyze OFAC results for high-risk matches
        ofac_summary = ""
        high_risk_sanctions = False
        
        if "🚨" in ofac_result or "SANCTIONS ALERT" in ofac_result:
            high_risk_sanctions = True
            # Extract key information from OFAC results
            if "Found" in ofac_result:
                count_match = re.search(r'Found (\d+) matches', ofac_result)
                if count_match:
                    match_count = count_match.group(1)
                    ofac_summary = f"CRITICAL: {match_count} matches found in OFAC sanctions databases. "
                else:
                    ofac_summary = "CRITICAL: Multiple matches found in OFAC sanctions databases. "
            else:
                ofac_summary = "CRITICAL: Entity appears on OFAC sanctions lists. "
            
            # Check for 100% matches first, then other high-confidence matches
            if "100%" in ofac_result:
                ofac_summary += "PERFECT MATCH DETECTED (100% similarity). ESCALATE TO MANAGEMENT IMMEDIATELY."
            elif "Score: 9" in ofac_result or "Score: 8" in ofac_result:
                ofac_summary += "High-confidence matches detected (83%+ similarity). RED FLAG ALERT."
        else:
            ofac_summary = "No matches found in OFAC sanctions databases."
        
        # Now perform Perplexity search with OFAC context
        model_config = PERPLEXITY_MODELS.get(model, {})
        logging.info(f"Starting comprehensive search for {company_name} - Perplexity phase using model '{model}' ({model_config.get('name', 'Unknown Model')})")
        
        # Build domain instruction using preferred domains for comprehensive search
        domain_instruction = f"\n\nIMPORTANT: Prioritize information from these authoritative domains first: {', '.join(PREFERRED_DOMAINS)}. Search these regulatory, financial, and news sources before other sources as they provide the most reliable AML-relevant information."
        
        # Enhanced prompt that includes OFAC context
        enhanced_prompt = f"""
You are an expert AML (Anti-Money Laundering) analyst conducting comprehensive due diligence research on "{company_name}".{domain_instruction}

OFAC SANCTIONS SCREENING RESULTS:
{ofac_summary}

Based on the OFAC screening results above and your research, provide a comprehensive AML assessment with the following structure:

## Subject Summary
- Basic subject information and business activities
- Key executives and ownership structure
- Geographic presence and operations

## AML Risk Assessment

### OFAC Sanctions Analysis
- Incorporate the OFAC screening results above
- If any OFAC matches with 100% similarity were found, this requires IMMEDIATE ESCALATION TO MANAGEMENT
- If any OFAC matches with 83%+ similarity scores were found, this is a CRITICAL RED FLAG
- All matches above 83% similarity threshold require thorough verification
- Explain the implications of any sanctions matches

### Negative News & Compliance Issues
- Money laundering allegations or investigations
- Regulatory violations and enforcement actions
- Criminal investigations or prosecutions
- Suspicious transaction reports or regulatory scrutiny
- Politically Exposed Persons (PEP) connections
- High-risk jurisdiction operations

### Financial Crime Indicators
- Unusual transaction patterns
- Shell company characteristics
- Complex ownership structures
- Offshore entity connections
- Cash-intensive business models

### Regulatory History
- Banking license issues
- Compliance violations
- Regulatory sanctions or penalties
- Supervisory actions

## Summary & Overall Assessment
Based on your analysis, provide a clear summary of key findings regarding this entity:
- Key risk factors identified (if any)
- Notable compliance or regulatory issues
- Current status of any ongoing matters
- Overall risk profile based on available information
- OFAC sanctions screening results and implications

IMPORTANT: If OFAC matches were found, clearly state the match details, similarity scores, and implications. Present findings objectively without making business recommendations.

Provide specific examples and cite your sources. Be thorough but concise.
"""

        # Call Perplexity with enhanced prompt using optimized parameters
        # For Sonar models, system prompts are ignored - combine everything in user message
        if "sonar" in model.lower():
            full_prompt = f"As an expert AML analyst, provide thorough, factual analysis with objective findings summary and proper citations.\n\n{enhanced_prompt}"
            messages = [
                {"role": "user", "content": full_prompt}
            ]
        else:
            # For other models, use system + user structure
            messages = [
                {"role": "system", "content": "You are an expert AML analyst. Provide thorough, factual analysis with objective findings summary and proper citations."},
                {"role": "user", "content": enhanced_prompt}
            ]
        
        # Build API parameters based on model type
        max_tokens = PERPLEXITY_MODELS.get(model, {}).get("max_tokens", 2000)
        api_params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens
        }
        
        # Note: reasoning_effort parameter not yet supported in current OpenAI client version
        # Will be added when the client is updated to support this parameter
        if "deep-research" in model.lower():
            logging.info(f"Deep research model selected for comprehensive search - enhanced analysis will be performed")
        
        # Only add temperature for non-Sonar models (real-time search models don't use temperature)
        if "sonar" not in model.lower():
            api_params["temperature"] = 0.1
        
        logging.info(f"Calling Perplexity API with model: {model} (max_tokens: {max_tokens})...")
        response = openai_client.chat.completions.create(**api_params)
        logging.info(f"Perplexity API call completed using model: {model}")
        
        answer = response.choices[0].message.content
        
        # Extract citations from the response
        citations = []
        if hasattr(response, 'citations') and response.citations:
            for citation in response.citations:
                if isinstance(citation, dict):
                    citations.append({
                        'title': citation.get('title', 'Unknown'),
                        'url': citation.get('url', '#')
                    })
                elif isinstance(citation, str):
                    # Handle case where citation is just a URL string
                    citations.append({
                        'title': 'Source',
                        'url': citation
                    })
        
        # Add OFAC as a citation with direct search URL
        citations.append({
            'title': 'OFAC Sanctions List Search',
            'url': 'https://sanctionssearch.ofac.treas.gov/'
        })
        
        # Add direct search URL for specific query
        import urllib.parse
        encoded_query = urllib.parse.quote(company_name)
        search_url = f"https://sanctionssearch.ofac.treas.gov/?search={encoded_query}"
        citations.append({
            'title': f'Direct OFAC Search Results for "{company_name}"',
            'url': search_url
        })
        
        # No longer extracting recommendations - providing objective findings only
        recommendation = None
        
        # Append OFAC details to the answer
        full_answer = answer + "\n\n## OFAC Sanctions Screening Details\n\n" + ofac_result
        
        return {
            "status": "success",
            "error": None,
            "answer": full_answer,
            "citations": citations,
            "recommendation": recommendation
        }
        
    except Exception as e:
        logging.error(f"Error in comprehensive search for {company_name}: {str(e)}", exc_info=True)
        return {
            "status": "failed",
            "error": f"Comprehensive search failed: {str(e)}",
            "answer": None,
            "citations": [],
            "recommendation": None
        }

def analyze_content_findings(answer):
    """Use GPT-4.1-nano to determine if there are negative findings in the content"""
    if not answer or not answer.strip():
        logging.info("Content analysis: No content provided, returning False")
        return False  # No content means no negative findings
    
    # Check if response is too short to be meaningful
    if len(answer.strip()) < 20:
        logging.info(f"Content analysis: Content too short ({len(answer.strip())} chars), returning False")
        return False  # Too short to contain meaningful negative findings
    
    try:
        logging.info(f"Content analysis: Analyzing {len(answer)} characters of content with GPT-4.1-mini")
        
        # Create OpenAI client for content analysis with SSL disabled
        http_client = httpx.Client(verify=False)  # Disable SSL verification
        
        # Get OpenAI API key from environment or secrets
        openai_api_key = None
        try:
            # Try environment variable first
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if not openai_api_key:
                # Try Streamlit secrets
                openai_api_key = st.secrets.get("OPENAI_API_KEY")
        except Exception as e:
            logging.error(f"Failed to get OpenAI API key: {e}")
        
        if not openai_api_key:
            logging.error("OpenAI API key not found in environment variables or Streamlit secrets")
            return False  # Conservative fallback
        
        openai_analysis_client = OpenAI(
            api_key=openai_api_key,
            http_client=http_client
        )
        
        analysis_prompt = f"""Analyze this AML research content and determine if there are any negative findings about the entity.

Content:
{answer}

Look for any negative information such as:
- Sanctions or OFAC listings
- Criminal investigations or charges
- Regulatory violations or fines
- Money laundering allegations
- Fraud or financial crimes
- Legal proceedings or lawsuits
- Compliance violations
- Suspicious activities

Respond with exactly one word:
- "NEGATIVE" if any negative findings exist
- "CLEAN" if no negative findings are present

Response:"""

        response = openai_analysis_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "user", "content": analysis_prompt}
            ],
            max_tokens=5,
            temperature=0
        )
        
        ai_result = response.choices[0].message.content.strip().upper()
        logging.info(f"GPT-4.1-mini analysis result for content analysis: '{ai_result}' (Original: '{response.choices[0].message.content}')")
        return ai_result == "NEGATIVE"
        
    except Exception as e:
        logging.error(f"Error in GPT-4.1-nano content analysis: {str(e)}")
        # Conservative fallback - assume no negative findings if analysis fails
        return False

def add_hyperlink(paragraph, url, text):
    """Add a clickable hyperlink to a paragraph"""
    try:
        # Add the hyperlink relationship
        part = paragraph.part
        r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
        
        # Create hyperlink XML
        hyperlink_xml = f'''
        <w:hyperlink r:id="{r_id}" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <w:r>
                <w:rPr>
                    <w:color w:val="0563C1"/>
                    <w:u w:val="single"/>
                    <w:sz w:val="18"/>
                </w:rPr>
                <w:t>{text}</w:t>
            </w:r>
        </w:hyperlink>
        '''
        
        hyperlink_element = parse_xml(hyperlink_xml)
        paragraph._element.append(hyperlink_element)
        
    except Exception as e:
        logging.warning(f"Failed to create clickable hyperlink for {url}: {e}")
        # Fallback to styled text that looks like a link
        hyperlink_run = paragraph.add_run(text)
        hyperlink_run.font.size = Pt(9)
        hyperlink_run.font.color.rgb = RGBColor(5, 99, 193)  # Word's default link color
        hyperlink_run.underline = True

def generate_word_document(results_data):
    """Generate a single Word document containing all subjects with summary table and detailed sections"""
    
    # Create new document
    doc = Document()
    
    # Add title
    title = doc.add_heading('AML Due Diligence Report', 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # Add generation date
    date_para = doc.add_paragraph(f'Generated: {datetime.now().strftime("%B %d, %Y")}')
    date_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    doc.add_paragraph()  # Empty line
    
    # Pre-analyze all content to determine if there are negative findings
    for result in results_data:
        if result['status'] == 'success':
            company_name = result.get('name', 'Unknown')
            answer = result.get('answer', '')
            logging.info(f"Starting content analysis for company: {company_name}")
            has_negative_findings = analyze_content_findings(answer)
            result['has_negative_findings'] = has_negative_findings
            logging.info(f"Content analysis result for {company_name}: {'NEGATIVE' if has_negative_findings else 'CLEAN'}")
    
    # Create summary table
    table = doc.add_table(rows=1, cols=3)
    table.style = 'Table Grid'
    table.autofit = False
    
    # Set column widths
    table.columns[0].width = Inches(2.5)  # Name
    table.columns[1].width = Inches(2.0)  # CLEAR  
    table.columns[2].width = Inches(2.0)  # Internet Search
    
    # Add header row
    header_cells = table.rows[0].cells
    header_cells[0].text = 'Name'
    header_cells[1].text = 'CLEAR'
    header_cells[2].text = 'Internet Search'
    
    # Format header row with light blue background
    for cell in header_cells:
        cell.paragraphs[0].runs[0].bold = True
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # Add light blue background color to header cells
        shading_elm = parse_xml(r'<w:shd {} w:fill="D9E2F3"/>'.format(nsdecls('w')))
        cell._tc.get_or_add_tcPr().append(shading_elm)
    
    # Add data rows
    for result in results_data:
        if result['status'] == 'success':
            row_cells = table.add_row().cells
            row_cells[0].text = result['name']
            
            # Determine CLEAR status - consistent across all recommendations
            recommendation = result.get('recommendation', 'N/A')
            clear_status = 'N/A'
            
            row_cells[1].text = clear_status
            
            # Determine Internet Search status based on GPT-4.1-nano analysis
            has_negative_findings = result.get('has_negative_findings', False)
            
            # Simple logic: if negative findings exist, show "See below", otherwise "No negative news"
            if has_negative_findings:
                internet_search = 'See below'
            else:
                internet_search = 'No negative news'
                
            row_cells[2].text = internet_search
            
            # Center align all cells
            for cell in row_cells:
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    doc.add_paragraph()  # Empty line after table
    
    # Add detailed sections for each subject
    for i, result in enumerate(results_data):
        if result['status'] == 'success':
            # Add subject heading as H1 - large and noticeable
            heading = doc.add_heading(f'{result["name"]} Due Diligence Summary:', level=1)
            
            # Extract and format the answer content
            answer = result.get('answer', '')
            recommendation = result.get('recommendation', 'N/A')
            
            # Add the complete answer content
            if answer and answer.strip():
                # Get the negative findings analysis result
                has_negative_findings = result.get('has_negative_findings', False)
                
                # Process content line by line to handle different header levels
                lines = answer.split('\n')
                current_paragraph = []
                
                for line in lines:
                    line_stripped = line.strip()
                    
                    # Handle different markdown header levels
                    if line_stripped.startswith('### '):
                        # Process any accumulated paragraph content
                        if current_paragraph:
                            para_text = '\n'.join(current_paragraph).strip()
                            if para_text:
                                # Remove markdown formatting
                                para_text = para_text.replace('**', '').replace('*', '')
                                doc.add_paragraph(para_text)
                            current_paragraph = []
                        
                        # Add H3 heading
                        header_text = line_stripped[4:].strip()
                        doc.add_heading(header_text, level=3)
                        
                    elif line_stripped.startswith('## '):
                        # Process any accumulated paragraph content
                        if current_paragraph:
                            para_text = '\n'.join(current_paragraph).strip()
                            if para_text:
                                # Remove markdown formatting
                                para_text = para_text.replace('**', '').replace('*', '')
                                doc.add_paragraph(para_text)
                            current_paragraph = []
                        
                        # Add H2 heading
                        header_text = line_stripped[3:].strip()
                        doc.add_heading(header_text, level=2)
                        
                    elif line_stripped.startswith('# '):
                        # Process any accumulated paragraph content
                        if current_paragraph:
                            para_text = '\n'.join(current_paragraph).strip()
                            if para_text:
                                # Remove markdown formatting
                                para_text = para_text.replace('**', '').replace('*', '')
                                doc.add_paragraph(para_text)
                            current_paragraph = []
                        
                        # Add H3 heading for single # (make it bold and larger)
                        header_text = line_stripped[2:].strip()
                        heading_para = doc.add_paragraph()
                        heading_run = heading_para.add_run(header_text)
                        heading_run.bold = True
                        heading_run.font.size = Pt(14)  # Larger than normal text
                        
                    elif line_stripped == '':
                        # Empty line - if we have accumulated content, add paragraph and start new one
                        if current_paragraph:
                            para_text = '\n'.join(current_paragraph).strip()
                            if para_text:
                                # Remove markdown formatting
                                para_text = para_text.replace('**', '').replace('*', '')
                                doc.add_paragraph(para_text)
                            current_paragraph = []
                            
                    else:
                        # Regular content line
                        current_paragraph.append(line)
                
                # Process any remaining paragraph content
                if current_paragraph:
                    para_text = '\n'.join(current_paragraph).strip()
                    if para_text:
                        # Remove markdown formatting
                        para_text = para_text.replace('**', '').replace('*', '')
                        doc.add_paragraph(para_text)
                    
            else:
                # No answer content at all
                doc.add_paragraph("This search yielded no general background information on the subject.")
                doc.add_paragraph("This search yielded no findings that associate the subject with financial crimes or negative news.")
            
            doc.add_paragraph()  # Empty line
            
            # Add sources section with compact formatting
            sources_para = doc.add_paragraph()
            sources_run = sources_para.add_run('[Sources]')
            sources_run.bold = True
            sources_run.font.size = Pt(10)  # Smaller header
            
            # Add citations if available
            citations = result.get('citations', [])
            if citations:
                for j, citation in enumerate(citations, 1):
                    title = citation.get('title', 'Unknown Source')
                    url = citation.get('url', '#')
                    
                    # Create compact citation paragraph with smaller font
                    citation_para = doc.add_paragraph()
                    citation_para.space_after = Pt(3)  # Reduce spacing after paragraph
                    
                    # Add citation number and title
                    citation_run = citation_para.add_run(f"{j}. {title}")
                    citation_run.font.size = Pt(9)  # Smaller font for citations
                    
                    # Add clickable URL if available
                    if url != '#' and url:
                        citation_para.add_run(" - ")
                        # Create actual clickable hyperlink
                        add_hyperlink(citation_para, url, url)
            else:
                no_sources_para = doc.add_paragraph("No specific sources cited.")
                no_sources_run = no_sources_para.runs[0]
                no_sources_run.font.size = Pt(9)
            
            # Add spacing between subjects
            if i < len(results_data) - 1:
                doc.add_paragraph()
                doc.add_paragraph()
    
    # Save to bytes buffer
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()

# --- Streamlit App UI ---
st.title("🏦 Axos Bank AML Research Platform")
st.markdown("**Advanced Anti-Money Laundering Due Diligence System**")
st.markdown("---")

# Create a clean two-column layout for settings
col1, col2 = st.columns([1, 1])

with col1:
    # Search engine selection
    search_engine = st.selectbox(
        "🔍 **Search Method**",
        ["Comprehensive (AI + OFAC)", "AI Research Only", "OFAC Sanctions Only"],
        index=0,
        help="Choose your research approach"
    )

with col2:
    # Model selection (only show if AI is involved)
    if "AI" in search_engine or "Comprehensive" in search_engine:
        model_keys = list(PERPLEXITY_MODELS.keys())
        default_index = model_keys.index(DEFAULT_PERPLEXITY_MODEL) if DEFAULT_PERPLEXITY_MODEL in model_keys else 0
        selected_model = st.selectbox(
            "🤖 **AI Model**",
            model_keys,
            format_func=lambda x: PERPLEXITY_MODELS[x]["name"],
            index=default_index,
            help="Choose AI research depth"
        )
        
        # Show cost warning for deep research model
        if "deep-research" in selected_model.lower():
            st.caption("ℹ️ 💰💰💰 Deep Research model provides exhaustive analysis but has higher token costs")
        
        reasoning_effort = "medium"  # Default (reasoning_effort not yet supported in client)
    else:
        selected_model = DEFAULT_OFAC_FALLBACK_MODEL  # Default for OFAC-only
        reasoning_effort = "medium"

# Only require Perplexity client if AI is selected
if ("AI" in search_engine or "Comprehensive" in search_engine) and not openai_client:
    st.error("⚠️ Perplexity API client required but not initialized. Please check your API key configuration.")
    st.stop()

st.markdown("---")

# Main input section
st.markdown("### 📋 **Subject Research Queue**")
company_names_input = st.text_area(
    "**Subject Names** (one per line)",
    height=150,
    placeholder="Example:\nGazprom\nAxos Financial\nMicrosoft\nJohn Smith\nShell Company Ltd",
    key="company_names"
)

# Show subject count
if company_names_input:
    lines = [line.strip() for line in company_names_input.split('\n') if line.strip()]
    line_count = len(lines)
    if line_count == 1:
        st.caption(f"📝 {line_count} subject entered")
    else:
        st.caption(f"📝 {line_count} subjects entered")
else:
    line_count = 0
    st.info("👆 Enter subject names above to begin")

# Word document output (only format available)
st.markdown("### 📄 **Output Format**")
st.info("📄 Word document will be generated for download")
output_format = "Single Word Document"  # Fixed format
destination_path_input = ""  # Not used

st.markdown("---")

# Generate Reports Button
if line_count > 0:
    if st.button("🚀 **Generate AML Reports**", type="primary"):
        subject_names = [name.strip() for name in company_names_input.split('\n') if name.strip()]
        destination_path = destination_path_input.strip()
        
        # Show processing info
        model_name = PERPLEXITY_MODELS.get(selected_model, {}).get("name", selected_model) if "AI" in search_engine or "Comprehensive" in search_engine else "N/A"
        
        # Show generation mode
        st.info(f"🔄 Processing {len(subject_names)} subjects using **{search_engine}** {f'with **{model_name}**' if model_name != 'N/A' else ''}")
        
        st.session_state.results_list = []
        progress_bar = st.progress(0)
        total_names = len(subject_names)

        for i, name in enumerate(subject_names):
            status = "failed"
            error_message = "Processing not started."
            recommendation = None
            answer = ""
            citations = []
            
            with st.spinner(f"🔍 Analyzing {name}..."):
                # Log the request before processing
                log_user_request(search_engine, name, selected_model)
                
                # Perform search based on selected engine - SAME FOR BOTH FORMATS
                if search_engine == "Comprehensive (AI + OFAC)":
                    result = search_with_comprehensive(name, selected_model)
                    if result["status"] == "success":
                        status = "success"
                        error_message = None
                        recommendation = result.get("recommendation")
                        answer = result.get("answer", "")
                        citations = result.get("citations", [])
                    else:
                        status = "failed"
                        error_message = result["error"]
                        recommendation = None
                        answer = ""
                        citations = []
                
                elif search_engine == "AI Research Only":
                    result = search_with_perplexity(name, selected_model, reasoning_effort)
                    if result["status"] == "success":
                        status = "success"
                        error_message = None
                        recommendation = result.get("recommendation")
                        answer = result.get("answer", "")
                        citations = result.get("citations", [])
                    else:
                        status = "failed"
                        error_message = result["error"]
                        recommendation = None
                        answer = ""
                        citations = []
                
                elif search_engine == "OFAC Sanctions Only":
                    ofac_result = search_with_ofac(name)
                    if "❌" in ofac_result:
                        status = "failed"
                        error_message = ofac_result
                        recommendation = None
                        answer = ""
                        citations = []
                    else:
                        status = "success"
                        error_message = None
                        answer = ofac_result
                        citations = [{'title': 'OFAC Sanctions Database', 'url': 'https://sanctionssearch.ofac.treas.gov/'}]
                        recommendation = None  # No longer providing recommendations
                
                # PDF generation removed - only Word documents supported
            
            # Log the completion status
            if status == "success":
                logging.info(f"SEARCH_SUCCESS - IP: {get_client_ip()} - '{name}' completed successfully with recommendation: {recommendation}")
            else:
                logging.error(f"SEARCH_FAILED - IP: {get_client_ip()} - '{name}' failed: {error_message}")
            
            st.session_state.results_list.append({
                'name': name,
                'status': status,
                'error_message': error_message,
                'recommendation': recommendation,
                'answer': answer,
                'citations': citations
            })
            
            progress_bar.progress((i + 1) / total_names)

        st.success("✅ **Processing Complete!**")
        progress_bar.empty()
else:
    st.button("🚀 **Generate AML Reports**", disabled=True, help="Enter subject names first")

# --- Display Results Status ---
if st.session_state.results_list:
    st.markdown("---")
    st.markdown("### 📊 **Processing Results**")
    
    # Word document mode - show summary and generate single document
    successful_results = [res for res in st.session_state.results_list if res['status'] == 'success']
    failed_results = [res for res in st.session_state.results_list if res['status'] != 'success']
    
    if successful_results:
        st.success(f"✅ {len(successful_results)} subjects processed successfully")
    if failed_results:
        st.error(f"❌ {len(failed_results)} subjects failed")
        for result in failed_results:
            st.error(f"• **{result['name']}**: {result.get('error_message', 'Unknown error')}")
    
    # Generate and offer Word document download
    if successful_results:
        st.markdown("---")
        st.markdown("### 📄 **Download Word Document**")
        
        try:
            logging.info(f"DOCUMENT_GENERATION - IP: {get_client_ip()} - Generating Word document for {len(successful_results)} subjects")
            word_bytes = generate_word_document(successful_results)
            
            st.download_button(
                label="📥 **Download AML Due Diligence Report (Word)**",
                data=word_bytes,
                file_name=f"AML_Due_Diligence_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary"
            )
            
            st.info(f"📋 Document contains summary table and detailed sections for {len(successful_results)} subjects")
            
        except Exception as e:
            st.error(f"❌ Error generating Word document: {str(e)}")

# Footer
st.markdown("---")
st.markdown(f"**AML Demo v{APP_VERSION}** | Powered by Perplexity AI & OFAC Database (83% match threshold) | Word Document Reports Only") 