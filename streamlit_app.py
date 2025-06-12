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

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import ReportLab components
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
import base64

# --- Initialize Session State (add this near the top) ---
if 'results_list' not in st.session_state:
    st.session_state.results_list = [] # Initialize if not already present

# --- Page Config (MUST be the first Streamlit command) ---
st.set_page_config(
    page_title="Axos Internal AML Demo", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Version number
APP_VERSION = "1.16"

# Configure logging (optional for Streamlit, but can be helpful)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

# --- Helper function for Grade Color ---
def get_grade_color(grade):
    if grade == 'A': return "green"
    if grade == 'B': return "#90EE90" # lightgreen
    if grade == 'C': return "orange"
    if grade == 'D': return "#FF4500" # orangered
    if grade == 'F': return "red"
    return "grey"
# --- End Helper Function ---

# --- Helper function for Inline Markdown (Bold/Italic) ---
def apply_inline_markdown(text):
    """Convert basic markdown to ReportLab-compatible HTML, handling line breaks properly"""
    # Convert **bold** -> <b>bold</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # Convert *italic* -> <i>italic</i>
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    # Handle bullet points and numbered lists
    lines = text.split('\n')
    formatted_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            formatted_lines.append('<br/>')
            continue
            
        # Handle numbered lists (e.g., "1. Item")
        if re.match(r'^\d+\.\s+', line):
            formatted_lines.append(f'<br/>{line}')
        # Handle bullet points
        elif line.startswith('- ') or line.startswith('• '):
            formatted_lines.append(f'<br/>{line}')
        # Handle OFAC entries that start with numbers and names
        elif re.match(r'^\*\*\d+\.\s+', line):
            formatted_lines.append(f'<br/><br/>{line}')
        # Handle indented lines (like OFAC details)
        elif line.startswith('   '):
            formatted_lines.append(f'<br/>{line}')
        # Handle emoji indicators
        elif any(emoji in line for emoji in ['🚨', '✅', '❌', '📍', '🏢', '📋', '📝', '🎯']):
            formatted_lines.append(f'<br/>{line}')
        else:
            formatted_lines.append(line)
    
    result = ' '.join(formatted_lines)
    
    # Clean up multiple consecutive <br/> tags
    result = re.sub(r'(<br/>){3,}', '<br/><br/>', result)
    
    # Escape remaining special characters
    result = result.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    # Restore the HTML tags we want to keep
    result = result.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')
    result = result.replace('&lt;i&gt;', '<i>').replace('&lt;/i&gt;', '</i>')
    result = result.replace('&lt;br/&gt;', '<br/>')
    
    return result
# --- End Helper Function ---

# --- Constants ---
NEGATIVE_KEYWORDS = '(arrest OR bankruptcy OR BSA OR conviction OR criminal OR fraud OR trafficking OR lawsuit OR "money laundering" OR OFAC OR Ponzi OR terrorist OR violation OR "honorary consul" OR consul OR "Panama Papers" OR theft OR corruption OR bribery)'

# Perplexity Models
PERPLEXITY_MODELS = {
    "sonar-pro": {
        "name": "Sonar Pro",
        "description": "Fast, efficient AI search with real-time web access",
        "use_case": "Standard research and analysis"
    },
    "sonar-deep-research": {
        "name": "Sonar Deep Research", 
        "description": "Exhaustive research across hundreds of sources with expert-level analysis",
        "use_case": "Comprehensive reports and detailed investigations"
    }
}

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

# --- Core Functions (Adapted from Flask app) ---

def search_with_perplexity(company_name, model="sonar-pro"):
    # (This function remains largely the same as in app.py)
    # ... (API call logic, prompt, message structure) ...
    logging.info(f"Starting Perplexity search for company: {company_name} using model: {model}")
    if not openai_client:
        logging.error("OpenAI client (for Perplexity) not initialized.")
        return {"status": "failed", "error": "Perplexity API client not initialized.", "answer": None, "citations": [], "aml_grade": None}
    try:
        # Updated Prompt: Ask for explicit separation with headings
        prompt = (
            f"First, on a single line, provide an Anti-Money Laundering (AML) risk grade for the company '{company_name}' based *only* on the negative news search results below. Use a scale from A (very low risk) to F (very high risk). Format this line ONLY as: 'AML Risk Grade: [GRADE]'. "
            f"\n\nThen provide a section clearly titled (bold text) 'Company Summary' with a brief summary of the company '{company_name}'. Insert two line breaks after the summary.  "
            f"\n\nAfter the summary, provide a section clearly titled (bold text) 'Negative News Findings' summarizing any negative news found regarding this company, focusing *only* on the following keywords: {NEGATIVE_KEYWORDS}. "
            f"\n\nFor the negative news findings, organize them into clear categories such as 'Financial Crimes', 'Regulatory Issues', 'Legal Proceedings', etc. For each finding, include when it happened, key parties involved, and current status if available. If no relevant negative news is found in a category, state that clearly."
            f"\n\nUse double line breaks between paragraphs. Provide citations as numeric references like [1], [2] etc., within the text where applicable."
        )
        messages = [
            {
                "role": "system",
                "content": "You are an AI assistant performing company research. Provide an AML risk grade based *only* on specified negative keywords. Then summarize the company under the heading '## Company Summary'. For the '## Negative News Findings' section, organize information into clear categories like '### Financial Crimes', '### Regulatory Issues', '### Legal Proceedings', etc. Include dates, parties involved, and current status of each finding. Use numeric citations [1] and maintain clean formatting with proper section headers.",
            },
            {"role": "user", "content": prompt},
        ]
        
        # Add reasoning_effort for deep research model
        api_params = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4000 if model == "sonar-deep-research" else 2000
        }
        
        logging.info(f"Calling Perplexity API (model: {model})...")
        response = openai_client.chat.completions.create(**api_params)
        logging.info("Perplexity API call completed.")
        
        full_answer_content = None
        citations = []
        aml_grade = None
        if response.choices and len(response.choices) > 0:
            message = response.choices[0].message
            if message and message.content:
                full_answer_content = message.content
                match = re.match(r"AML Risk Grade: ([A-F])", full_answer_content, re.IGNORECASE)
                if match:
                    aml_grade = match.group(1).upper()
                    full_answer_content = re.sub(r"AML Risk Grade: [A-F]\n*", "", full_answer_content, count=1, flags=re.IGNORECASE).strip()
                else:
                     logging.warning("Could not extract AML Grade.")
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
            
        return {"status": "success", "error": None, "answer": full_answer_content, "citations": citations, "aml_grade": aml_grade}

    except Exception as e:
        logging.error(f"Error during Perplexity search for {company_name}: {str(e)}", exc_info=True)
        return {"status": "failed", "error": str(e), "answer": None, "citations": [], "aml_grade": None}

def search_with_ofac(query):
    """Search OFAC sanctions database"""
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
            'ctl00$MainContent$Slider1': '100',
            'ctl00$MainContent$Slider1_Boundcontrol': '100',
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
            return "✅ No matches found in OFAC sanctions database - entity appears clean"
        
        # Format the results
        result_text = f"🚨 **SANCTIONS ALERT**: Found {results_count} matches in OFAC database\n\n"
        
        # Show first few results as examples
        shown_results = 0
        max_show = min(5, len(rows) - 1)  # Show up to 5 results, excluding header
        
        for i, row in enumerate(rows[1:], 1):  # Skip header row
            if shown_results >= max_show:
                break
                
            cells = row.find_all('td')
            if len(cells) >= 6:
                name_cell = cells[0]
                name_link = name_cell.find('a')
                name = name_link.get_text().strip() if name_link else name_cell.get_text().strip()
                
                address = cells[1].get_text().strip()
                entity_type = cells[2].get_text().strip()
                programs = cells[3].get_text().strip()
                list_type = cells[4].get_text().strip()
                score = cells[5].get_text().strip()
                
                result_text += f"**{i}. {name}**\n"
                if address:
                    result_text += f"   📍 Address: {address}\n"
                result_text += f"   🏢 Type: {entity_type}\n"
                result_text += f"   📋 Programs: {programs}\n"
                result_text += f"   📝 List: {list_type}\n"
                result_text += f"   🎯 Match Score: {score}%\n\n"
                
                shown_results += 1
        
        if results_count > max_show:
            result_text += f"... and {results_count - max_show} more matches\n\n"
        
        result_text += "⚠️ **This entity appears on OFAC sanctions lists. Proceed with extreme caution.**"
        
        return result_text
        
    except requests.exceptions.Timeout:
        return "❌ OFAC search timed out. Please try again."
    except requests.exceptions.RequestException as e:
        return f"❌ Error connecting to OFAC database: {str(e)}"
    except Exception as e:
        return f"❌ Error during OFAC search: {str(e)}"

def generate_pdf_bytes(company_name, data, search_engine="Unknown"):
    """Generates the PDF content and returns it as bytes."""
    logging.info(f"Attempting to generate PDF bytes for {company_name}")
    buffer = io.BytesIO()
    try:
        doc = SimpleDocTemplate(buffer, pagesize=(8.5*inch, 11*inch), leftMargin=0.75*inch, rightMargin=0.75*inch, topMargin=0.75*inch, bottomMargin=0.75*inch)
        styles = getSampleStyleSheet()
        story = []

        # --- Add Axos Bank Logo ---
        try:
            # Create a simple text-based logo since SVG handling in ReportLab can be complex
            logo_style = ParagraphStyle(
                name='LogoStyle', 
                parent=styles['Normal'], 
                fontSize=16, 
                textColor=colors.HexColor('#2c4f7c'),
                alignment=TA_CENTER,
                spaceAfter=10,
                fontName='Helvetica-Bold'
            )
            story.append(Paragraph("a<font color='#f39c12'>x</font>os", logo_style))
            story.append(Spacer(1, 0.1*inch))
        except Exception as logo_error:
            logging.warning(f"Could not add logo: {logo_error}")
            # Continue without logo if there's an issue

        # --- AML Grade (same styling logic) ---
        aml_grade = data.get("aml_grade", "N/A")
        grade_color = colors.grey
        # ... (grade color assignment) ...
        if aml_grade == 'A': grade_color = colors.darkgreen
        elif aml_grade == 'B': grade_color = colors.green
        elif aml_grade == 'C': grade_color = colors.orange
        elif aml_grade == 'D': grade_color = colors.orangered
        elif aml_grade == 'F': grade_color = colors.darkred
        grade_style = ParagraphStyle(name='AMLGrade', parent=styles['h1'], fontSize=20, textColor=grade_color, alignment=TA_CENTER, spaceAfter=15)
        story.append(Paragraph(f"AML Risk: {aml_grade}", grade_style))

        # --- Title (same styling logic) ---
        title_style = styles['h1']
        title_style.alignment = TA_CENTER
        title_style.fontSize = 18
        story.append(Paragraph(f"Research Report: {company_name}", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # --- Search Engine Info ---
        search_engine_style = ParagraphStyle(name='SearchEngine', parent=styles['Normal'], fontSize=10, textColor=colors.grey, alignment=TA_CENTER, spaceAfter=10)
        story.append(Paragraph(f"Generated using: {search_engine}", search_engine_style))
        story.append(Spacer(1, 0.1*inch))

        # --- Summary & Analysis (Parse and Format Sections) ---
        answer_text = data.get("answer", "N/A").strip()
        
        # Define styles
        h2_style = styles['h2']
        h3_style = ParagraphStyle(name='H3', parent=styles['h2'], fontSize=12, spaceBefore=8, spaceAfter=4)
        body_style = ParagraphStyle(name='BodyText', parent=styles['Normal'], spaceBefore=6, spaceAfter=6, leading=14, fontSize=10, alignment=TA_LEFT)

        # Split content based on expected headings
        # Pattern now looks for both ## and ### headings
        parts = re.split(r'(^## .*$|^### .*$)', answer_text, flags=re.MULTILINE)
        
        # Filter out empty strings resulting from split
        parts = [p.strip() for p in parts if p and p.strip()]

        if len(parts) > 1: # If headings were found and split occurred
            current_heading_level = None
            for part in parts:
                if part.startswith('## '):
                    # Main heading (Company Summary or Negative News Findings)
                    heading_text = part.replace('## ', '')
                    current_heading_level = 2
                    story.append(Spacer(1, 0.1*inch))
                    story.append(Paragraph(heading_text, h2_style))
                    story.append(Spacer(1, 0.05*inch))
                elif part.startswith('### '):
                    # Subheading (categories like Financial Crimes, etc.)
                    heading_text = part.replace('### ', '')
                    current_heading_level = 3
                    story.append(Spacer(1, 0.1*inch))
                    story.append(Paragraph(heading_text, h3_style))
                    story.append(Spacer(1, 0.03*inch))
                else:
                    # This is the text content following a heading
                    formatted_text = apply_inline_markdown(part)
                    story.append(Paragraph(formatted_text, body_style))
        else:
            # Fallback: If headings weren't found, render the whole block
            logging.warning(f"Could not find expected headings in response for {company_name}. Rendering as plain block.")
            escaped_answer = answer_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(f'<pre>{escaped_answer}</pre>', body_style))

        story.append(Spacer(1, 0.2*inch))

        # --- Citations Section (improved formatting) ---
        story.append(Paragraph("Sources Cited", styles['h2']))
        story.append(Spacer(1, 0.1*inch))
        citations = data.get("citations", [])
        if citations:
            citation_style = ParagraphStyle(name='Citation', parent=styles['Normal'], fontSize=9, leading=11, spaceAfter=6)
            for i, citation in enumerate(citations):
                url = citation.get('url', '#')
                title = citation.get('title', url)
                
                # Extract website name from URL
                website_name = "Unknown Source"
                try:
                    from urllib.parse import urlparse
                    parsed_url = urlparse(url)
                    if parsed_url.netloc:
                        website_name = parsed_url.netloc.replace('www.', '')
                except:
                    website_name = "Unknown Source"
                
                # Format citation with website name and URL separately
                safe_title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                safe_url = url.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                safe_website = website_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                
                citation_text = f'<b>{i+1}. {safe_title}</b><br/>'
                citation_text += f'   Website: {safe_website}<br/>'
                citation_text += f'   URL: <font color="blue">{safe_url}</font>'
                
                story.append(Paragraph(citation_text, citation_style))
        else:
            story.append(Paragraph("None provided or embedded in text.", styles['Italic']))

        # Build the PDF in the buffer
        doc.build(story)
        logging.info(f"Successfully generated PDF bytes for {company_name}")
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as pdf_error:
        logging.error(f"Error generating PDF bytes for {company_name}: {pdf_error}", exc_info=True)
        return None

def search_with_comprehensive(company_name, model="sonar-pro"):
    """Comprehensive search combining Perplexity AI research with OFAC sanctions screening"""
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
            
            # Check for high-confidence matches (scores above 80%)
            if "100%" in ofac_result or "Score: 9" in ofac_result or "Score: 8" in ofac_result:
                ofac_summary += "High-confidence matches detected (80%+ similarity). RED FLAG ALERT."
        else:
            ofac_summary = "No matches found in OFAC sanctions databases."
        
        # Now perform Perplexity search with OFAC context
        logging.info(f"Starting comprehensive search for {company_name} - Perplexity phase using {model}")
        
        # Enhanced prompt that includes OFAC context
        enhanced_prompt = f"""
You are an expert AML (Anti-Money Laundering) analyst conducting comprehensive due diligence research on "{company_name}".

OFAC SANCTIONS SCREENING RESULTS:
{ofac_summary}

Based on the OFAC screening results above and your research, provide a comprehensive AML risk assessment covering:

## Company Summary
- Basic company information and business activities
- Key executives and ownership structure
- Geographic presence and operations

## AML Risk Assessment

### OFAC Sanctions Analysis
- Incorporate the OFAC screening results above
- If any OFAC matches with 80%+ similarity scores were found, this is a RED FLAG
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

## Risk Grade Assignment
Based on your analysis, assign ONE of these AML risk grades:
- A: Low Risk (Clean entity, no significant red flags)
- B: Low-Medium Risk (Minor concerns, manageable with standard controls)
- C: Medium Risk (Some concerns, enhanced due diligence recommended)
- D: High Risk (Significant concerns, extensive due diligence required)
- F: Critical Risk (Sanctions matches, criminal activity, or severe red flags - DO NOT PROCEED)

IMPORTANT: If OFAC matches with 80%+ similarity were found, the grade should be F regardless of other factors.

Provide specific examples and cite your sources. Be thorough but concise.
"""

        # Call Perplexity with enhanced prompt
        messages = [
            {"role": "system", "content": "You are an expert AML analyst. Provide thorough, factual analysis with proper citations."},
            {"role": "user", "content": enhanced_prompt}
        ]
        
        # Add reasoning_effort for deep research model
        api_params = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4000 if model == "sonar-deep-research" else 2000
        }
        
        logging.info(f"Calling Perplexity API (model: {model})...")
        response = openai_client.chat.completions.create(**api_params)
        
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
        
        # Add OFAC as a citation
        citations.append({
            'title': 'OFAC Sanctions List Search',
            'url': 'https://sanctionssearch.ofac.treas.gov/'
        })
        
        # Extract AML grade from response
        aml_grade = "N/A"
        grade_patterns = [
            r"(?:Risk Grade|AML Grade|Grade):\s*([A-F])",
            r"Grade:\s*([A-F])",
            r"Risk:\s*([A-F])",
            r"\b([A-F]):\s*(?:Low|Medium|High|Critical)"
        ]
        
        for pattern in grade_patterns:
            match = re.search(pattern, answer, re.IGNORECASE)
            if match:
                aml_grade = match.group(1).upper()
                break
        
        # Override grade if high-risk sanctions found
        if high_risk_sanctions:
            aml_grade = "F"
        
        # Append OFAC details to the answer
        full_answer = answer + "\n\n## OFAC Sanctions Screening Details\n\n" + ofac_result
        
        return {
            "status": "success",
            "error": None,
            "answer": full_answer,
            "citations": citations,
            "aml_grade": aml_grade
        }
        
    except Exception as e:
        logging.error(f"Error in comprehensive search for {company_name}: {str(e)}", exc_info=True)
        return {
            "status": "failed",
            "error": f"Comprehensive search failed: {str(e)}",
            "answer": None,
            "citations": [],
            "aml_grade": None
        }

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
        selected_model = st.selectbox(
            "🤖 **AI Model**",
            list(PERPLEXITY_MODELS.keys()),
            format_func=lambda x: PERPLEXITY_MODELS[x]["name"],
            index=0,
            help="Choose AI research depth"
        )
    else:
        selected_model = "sonar-pro"  # Default for OFAC-only

# Only require Perplexity client if AI is selected
if ("AI" in search_engine or "Comprehensive" in search_engine) and not openai_client:
    st.error("⚠️ Perplexity API client required but not initialized. Please check your API key configuration.")
    st.stop()

st.markdown("---")

# Main input section
st.markdown("### 📋 **Company Research Queue**")
company_names_input = st.text_area(
    "Enter company names (one per line)",
    height=120,
    placeholder="Example:\nGazprom\nAxos Financial\nMicrosoft\nShell Company Ltd",
    help="Add companies you want to research. Each company will get a detailed AML report."
)

# Show company count
if company_names_input:
    lines = [line.strip() for line in company_names_input.split('\n') if line.strip()]
    line_count = len(lines)
    if line_count > 0:
        st.success(f"✅ {line_count} companies queued for analysis")
else:
    line_count = 0
    st.info("👆 Enter company names above to begin")

# Optional local save path
with st.expander("⚙️ **Advanced Options**", expanded=False):
    destination_path_input = st.text_input(
        "Local Save Path (Optional)", 
        placeholder="C:\\Reports or /Users/name/Reports",
        help="Save PDFs directly to this folder when running locally"
    )
    if destination_path_input:
        st.caption("📁 PDFs will be saved locally if path is valid, otherwise downloaded as ZIP")

st.markdown("---")

# Generate Reports Button
if line_count > 0:
    if st.button("🚀 **Generate AML Reports**", type="primary"):
        company_names = [name.strip() for name in company_names_input.split('\n') if name.strip()]
        destination_path = destination_path_input.strip()
        
        # Validate destination path if provided
        save_locally = False
        if destination_path:
            if os.path.isdir(destination_path):
                save_locally = True
                st.success(f"📁 Reports will be saved to: {destination_path}")
            else:
                st.warning(f"⚠️ Invalid path: '{destination_path}' - Reports will be downloaded as ZIP")
                destination_path = ""
        
        # Show processing info
        model_name = PERPLEXITY_MODELS.get(selected_model, {}).get("name", selected_model) if "AI" in search_engine or "Comprehensive" in search_engine else "N/A"
        st.info(f"🔄 Processing {len(company_names)} companies using **{search_engine}** {f'with **{model_name}**' if model_name != 'N/A' else ''}")
        
        st.session_state.results_list = []
        progress_bar = st.progress(0)
        total_names = len(company_names)

        for i, name in enumerate(company_names):
            pdf_bytes = None
            status = "failed"
            error_message = "Processing not started."
            aml_grade = None
            save_location_message = ""
            
            with st.spinner(f"🔍 Analyzing {name}..."):
                # Perform search based on selected engine
                if search_engine == "Comprehensive (AI + OFAC)":
                    result = search_with_comprehensive(name, selected_model)
                    if result["status"] == "success":
                        pdf_bytes = generate_pdf_bytes(name, result, "Comprehensive")
                        status = "success"
                        error_message = None
                        aml_grade = result.get("aml_grade")
                    else:
                        status = "failed"
                        error_message = result["error"]
                        aml_grade = None
                        pdf_bytes = None
                
                elif search_engine == "AI Research Only":
                    result = search_with_perplexity(name, selected_model)
                    if result["status"] == "success":
                        pdf_bytes = generate_pdf_bytes(name, result, "AI Research")
                        status = "success"
                        error_message = None
                        aml_grade = result.get("aml_grade")
                    else:
                        status = "failed"
                        error_message = result["error"]
                        aml_grade = None
                        pdf_bytes = None
                
                elif search_engine == "OFAC Sanctions Only":
                    ofac_result = search_with_ofac(name)
                    if "❌" in ofac_result:
                        status = "failed"
                        error_message = ofac_result
                        aml_grade = None
                        pdf_bytes = None
                    else:
                        status = "success"
                        error_message = None
                        if "🚨" in ofac_result or "SANCTIONS ALERT" in ofac_result:
                            aml_grade = "F"
                        else:
                            aml_grade = "A"
                        pdf_bytes = generate_pdf_bytes(name, {"answer": ofac_result, "aml_grade": aml_grade}, "OFAC")
                
                # Handle local saving
                if status == "success" and pdf_bytes:
                    if save_locally and destination_path:
                        safe_company_name = "".join(c if c.isalnum() else '_' for c in name)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        pdf_filename = f"{safe_company_name}_{timestamp}.pdf"
                        filepath = os.path.join(destination_path, pdf_filename)
                        try:
                            with open(filepath, 'wb') as f:
                                f.write(pdf_bytes)
                            logging.info(f"Successfully saved PDF locally: {filepath}")
                            save_location_message = f"Saved to: {destination_path}"
                            pdf_bytes = None
                        except Exception as save_error:
                            logging.error(f"Failed to save PDF locally to {filepath}: {save_error}")
                            status = "warning"
                            error_message = f"PDF generated, but local save failed: {save_error}"
                            save_location_message = "Local save failed, see ZIP download"
                    elif pdf_bytes:
                        save_location_message = "Ready for ZIP download"
                    else:
                        save_location_message = "PDF generation failed"
                else:
                    save_location_message = "Processing failed"
            
            st.session_state.results_list.append({
                'name': name,
                'status': status,
                'error_message': error_message,
                'pdf_bytes': pdf_bytes,
                'aml_grade': aml_grade,
                'save_location_message': save_location_message
            })
            
            progress_bar.progress((i + 1) / total_names)

        st.success("✅ **Processing Complete!**")
        progress_bar.empty()
else:
    st.button("🚀 **Generate AML Reports**", disabled=True, help="Enter company names first")

# --- Display Results Status ---
if st.session_state.results_list:
    st.markdown("---")
    st.markdown("### 📊 **Processing Results**")
    
    pdfs_for_zip = []
    status_cols = st.columns(2)
    current_status_col = 0
    
    for result in st.session_state.results_list:
        with status_cols[current_status_col]:
            grade = result.get('aml_grade', 'N/A')
            save_msg = result.get('save_location_message', '')
            
            if result['status'] == 'success' and result.get('pdf_bytes') is None and save_msg:
                st.success(f"✅ **{result['name']}** [Risk: {grade}] - {save_msg}")
            elif result['status'] == 'success' and result.get('pdf_bytes') is not None:
                st.info(f"📄 **{result['name']}** [Risk: {grade}] - {save_msg}")
                pdfs_for_zip.append(result)
            elif result['status'] == 'warning':
                st.warning(f"⚠️ **{result['name']}** [Risk: {grade}] - {save_msg}")
                if result.get('pdf_bytes') is not None:
                    pdfs_for_zip.append(result)
            else:
                st.error(f"❌ **{result['name']}** - Failed ({result.get('error_message', 'Unknown error')})")
        current_status_col = 1 - current_status_col
    
    # ZIP Download Section
    if pdfs_for_zip:
        st.markdown("---")
        st.markdown("### 📦 **Download Reports**")
        st.info(f"💾 {len(pdfs_for_zip)} reports ready for download")
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
            for result in pdfs_for_zip:
                safe_name = "".join(c if c.isalnum() else '_' for c in result['name'])
                pdf_filename = f"{safe_name}_AML_Report.pdf"
                if result.get('pdf_bytes'):
                    zipf.writestr(pdf_filename, result['pdf_bytes'])
        
        zip_buffer.seek(0)
        
        st.download_button(
            label="📥 **Download All Reports (ZIP)**",
            data=zip_buffer,
            file_name=f"AML_Reports_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
            mime="application/zip",
            type="primary"
        )
    elif any(res['status'] == 'success' for res in st.session_state.results_list):
        st.success("🎉 All reports were saved directly to your local folder!")

# Footer
st.markdown("---")
st.markdown("**AML Demo v1.19** | Powered by Perplexity AI & OFAC Database") 