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
APP_VERSION = "1.20"

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

# --- Helper function for Recommendation Color ---
def get_recommendation_color(recommendation):
    if recommendation == 'PROCEED': return "green"
    if recommendation == 'ENHANCED DUE DILIGENCE': return "orange"
    if recommendation == 'HIGH RISK': return "#FF4500" # orangered
    if recommendation == 'DO NOT PROCEED': return "red"
    return "grey"
# --- End Helper Function ---

# --- Helper function for OFAC PDF Formatting ---
def format_ofac_for_pdf(text, story, styles):
    """Format OFAC results as separate PDF elements with proper spacing"""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.platypus import Spacer, Paragraph
    from reportlab.lib.units import inch
    
    lines = text.split('\n')
    
    # Define styles for OFAC formatting
    header_style = ParagraphStyle(
        name='OFACHeader', 
        parent=styles['Normal'], 
        fontSize=12, 
        fontName='Helvetica-Bold',
        spaceAfter=12,
        spaceBefore=6,
        textColor=colors.darkred if '🚨' in text else colors.darkgreen
    )
    
    subheader_style = ParagraphStyle(
        name='OFACSubheader', 
        parent=styles['Normal'], 
        fontSize=11, 
        fontName='Helvetica-Bold',
        spaceAfter=8,
        spaceBefore=8
    )
    
    body_style = ParagraphStyle(
        name='OFACBody', 
        parent=styles['Normal'], 
        fontSize=10,
        spaceAfter=4,
        leading=12
    )
    
    bullet_style = ParagraphStyle(
        name='OFACBullet', 
        parent=styles['Normal'], 
        fontSize=10,
        leftIndent=20,
        spaceAfter=3,
        leading=12
    )
    
    summary_style = ParagraphStyle(
        name='OFACSummary', 
        parent=styles['Normal'], 
        fontSize=10,
        fontName='Helvetica-Bold',
        spaceAfter=6,
        spaceBefore=8
    )
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
            
        # Handle main headers (🚨 SANCTIONS ALERT, ✅ OFAC CLEAR)
        if line.startswith('🚨') or line.startswith('✅'):
            # Remove markdown formatting for cleaner display
            clean_line = re.sub(r'\*\*(.*?)\*\*', r'\1', line)
            story.append(Paragraph(clean_line, header_style))
            story.append(Spacer(1, 0.1*inch))
            
        # Handle match threshold info
        elif line.startswith('*(Minimum match threshold'):
            clean_line = line.replace('*(', '(').replace(')*', ')')
            story.append(Paragraph(f"<i>{clean_line}</i>", body_style))
            story.append(Spacer(1, 0.15*inch))
            
        # Handle match headers (Match #1:, Match #2:, etc.)
        elif line.startswith('**Match #'):
            clean_line = re.sub(r'\*\*(.*?)\*\*', r'\1', line)
            story.append(Paragraph(clean_line, subheader_style))
            
            # Process the details for this match
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith('**Match #') and not lines[i].strip().startswith('*... and') and not lines[i].strip() == '---':
                detail_line = lines[i].strip()
                if detail_line.startswith('•'):
                    # Handle bullet points
                    clean_detail = re.sub(r'• \*\*(.*?)\*\*:', r'<b>\1</b>:', detail_line)
                    story.append(Paragraph(clean_detail, bullet_style))
                elif detail_line:
                    story.append(Paragraph(detail_line, bullet_style))
                i += 1
            story.append(Spacer(1, 0.1*inch))
            i -= 1  # Adjust for the outer loop increment
            
        # Handle separator lines
        elif line == '---':
            story.append(Spacer(1, 0.1*inch))
            # Add a simple line separator
            story.append(Paragraph("_" * 50, body_style))
            story.append(Spacer(1, 0.1*inch))
            
        # Handle summary and recommendation
        elif line.startswith('**Summary**:'):
            clean_line = re.sub(r'\*\*(.*?)\*\*:', r'<b>\1</b>:', line)
            story.append(Paragraph(clean_line, summary_style))
            
        elif line.startswith('**Recommendation**:'):
            clean_line = re.sub(r'\*\*(.*?)\*\*:', r'<b>\1</b>:', line)
            # Color code the recommendation
            if 'DO NOT PROCEED' in line:
                rec_style = ParagraphStyle(name='RecStyle', parent=summary_style, textColor=colors.darkred)
            elif 'ENHANCED DUE DILIGENCE' in line:
                rec_style = ParagraphStyle(name='RecStyle', parent=summary_style, textColor=colors.orange)
            else:
                rec_style = summary_style
            story.append(Paragraph(clean_line, rec_style))
            
        # Handle additional matches info
        elif line.startswith('*... and'):
            clean_line = line.replace('*', '')
            story.append(Paragraph(f"<i>{clean_line}</i>", body_style))
            story.append(Spacer(1, 0.1*inch))
            
        # Handle regular content lines
        else:
            if '**' in line:
                clean_line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
                story.append(Paragraph(clean_line, body_style))
            elif line and not line.isspace():
                story.append(Paragraph(line, body_style))
                
        i += 1

# --- Helper function for Inline Markdown (Bold/Italic) ---
def format_ofac_results(text):
    """Special formatter for OFAC results with proper structure and spacing"""
    lines = text.split('\n')
    formatted_lines = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
            
        # Handle main headers (🚨 SANCTIONS ALERT, ✅ OFAC CLEAR)
        if line.startswith('🚨') or line.startswith('✅'):
            formatted_lines.append(f'<br/><br/><b>{line}</b><br/>')
        # Handle match threshold info
        elif line.startswith('*(Minimum match threshold'):
            formatted_lines.append(f'<i>{line}</i><br/><br/>')
        # Handle match headers (Match #1:, Match #2:, etc.)
        elif line.startswith('**Match #'):
            formatted_lines.append(f'<br/><b>{line.replace("**", "")}</b><br/>')
        # Handle bullet points with proper indentation
        elif line.startswith('• **'):
            # Extract the key and value for better formatting
            formatted_line = line.replace('• **', '    • <b>').replace('**: ', '</b>: ')
            formatted_lines.append(f'{formatted_line}<br/>')
        # Handle separator lines
        elif line == '---':
            formatted_lines.append('<br/><hr/><br/>')
        # Handle summary and recommendation headers
        elif line.startswith('**Summary**:'):
            formatted_lines.append(f'<br/><b>Summary</b>: {line.replace("**Summary**: ", "")}<br/>')
        elif line.startswith('**Recommendation**:'):
            formatted_lines.append(f'<br/><b>Recommendation</b>: {line.replace("**Recommendation**: ", "")}<br/>')
        # Handle additional matches info
        elif line.startswith('*... and'):
            formatted_lines.append(f'<br/><i>{line}</i><br/>')
        # Handle any other bold text
        elif '**' in line:
            formatted_line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
            formatted_lines.append(f'{formatted_line}<br/>')
        # Handle regular lines
        else:
            if line and not line.isspace():
                formatted_lines.append(f'{line}<br/>')
    
    result = ''.join(formatted_lines)
    
    # Clean up excessive line breaks
    result = re.sub(r'(<br/>){3,}', '<br/><br/>', result)
    
    # Escape special characters but preserve our HTML tags
    result = result.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    # Restore the HTML tags we want to keep
    result = result.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')
    result = result.replace('&lt;i&gt;', '<i>').replace('&lt;/i&gt;', '</i>')
    result = result.replace('&lt;br/&gt;', '<br/>')
    result = result.replace('&lt;hr/&gt;', '<hr/>')
    
    return result

def apply_inline_markdown(text):
    """Convert basic markdown to ReportLab-compatible HTML, handling line breaks properly"""
    # Check if this is OFAC results and use special formatter
    if ('SANCTIONS ALERT' in text or 'OFAC CLEAR' in text or 'Match #' in text):
        return format_ofac_results(text)
    
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
            f"Provide a comprehensive AML (Anti-Money Laundering) due diligence assessment for '{company_name}'. "
            f"\n\nStructure your response as follows:"
            f"\n\n## Company Summary"
            f"\nProvide a brief summary of the company '{company_name}', including business activities, key executives, and geographic presence.\n"
            f"\n\n## AML Risk Assessment"
            f"\nAnalyze any negative news found regarding this company, focusing on: {NEGATIVE_KEYWORDS}. "
            f"Organize findings into clear categories such as 'Financial Crimes', 'Regulatory Issues', 'Legal Proceedings', etc. "
            f"For each finding, include when it happened, key parties involved, and current status if available. "
            f"If no relevant negative news is found in a category, state that clearly.\n"
            f"\n\n## Summary & Recommendation"
            f"\nProvide a clear summary of key risks identified and a specific recommendation on how to proceed with this entity. "
            f"Use one of these recommendation categories:"
            f"\n- **PROCEED**: Low risk, standard due diligence sufficient"
            f"\n- **ENHANCED DUE DILIGENCE**: Some concerns identified, additional review recommended"  
            f"\n- **HIGH RISK**: Significant concerns, extensive documentation and approval required"
            f"\n- **DO NOT PROCEED**: Critical risk factors present, avoid business relationship"
            f"\n\nUse double line breaks between sections. Provide citations as numeric references like [1], [2] etc., within the text where applicable."
        )
        messages = [
            {
                "role": "system",
                "content": "You are an expert AML analyst performing company due diligence. Provide comprehensive analysis with clear company summary, risk assessment with organized categories, and specific recommendations. Use numeric citations [1] and maintain clean formatting with proper section headers.",
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
        recommendation = None
        if response.choices and len(response.choices) > 0:
            message = response.choices[0].message
            if message and message.content:
                full_answer_content = message.content
                # Extract recommendation from the content
                rec_patterns = [
                    r"\*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*",
                    r"Recommendation[:\s]*\*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*",
                    r"- \*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*"
                ]
                
                for pattern in rec_patterns:
                    match = re.search(pattern, full_answer_content, re.IGNORECASE)
                    if match:
                        recommendation = match.group(1).upper()
                        break
                
                if not recommendation:
                    logging.warning("Could not extract recommendation from response.")
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
            return "✅ **OFAC CLEAR**: No matches found in OFAC sanctions database\n\n**Summary**: Entity appears clean from sanctions perspective (minimum 83% match threshold).\n\n**Recommendation**: Proceed with standard due diligence protocols."
        
        # Format the results with better spacing and structure
        result_text = f"🚨 **SANCTIONS ALERT**: Found {results_count} potential match{'es' if results_count > 1 else ''} in OFAC database\n"
        result_text += f"*(Minimum match threshold: 83%)*\n\n"
        
        # Show first few results as examples
        shown_results = 0
        max_show = min(5, len(rows) - 1)  # Show up to 5 results, excluding header
        high_confidence_matches = 0
        
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
                
                # Track high confidence matches
                try:
                    score_num = float(score.replace('%', ''))
                    if score_num >= 95:
                        high_confidence_matches += 1
                except:
                    pass
                
                result_text += f"**Match #{shown_results + 1}: {name}**\n"
                result_text += f"• **Match Score**: {score}%\n"
                if address:
                    result_text += f"• **Address**: {address}\n"
                result_text += f"• **Entity Type**: {entity_type}\n"
                result_text += f"• **Programs**: {programs}\n"
                result_text += f"• **List**: {list_type}\n"
                result_text += f"\n"
                
                shown_results += 1
        
        if results_count > max_show:
            result_text += f"*... and {results_count - max_show} additional match{'es' if results_count - max_show > 1 else ''}*\n\n"
        
        # Add summary and recommendation
        result_text += "---\n\n"
        if high_confidence_matches > 0:
            result_text += f"**Summary**: {high_confidence_matches} high-confidence match{'es' if high_confidence_matches > 1 else ''} (95%+) detected. Entity has strong similarity to sanctioned individuals/entities.\n\n"
            result_text += "**Recommendation**: ⛔ **DO NOT PROCEED** - Conduct thorough manual review and legal consultation before any business relationship."
        else:
            result_text += f"**Summary**: {results_count} potential match{'es' if results_count > 1 else ''} found with 83%+ similarity. Manual review required to determine false positives.\n\n"
            result_text += "**Recommendation**: ⚠️ **ENHANCED DUE DILIGENCE** - Manually verify each match and document decision rationale."
        
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

        # --- AML Recommendation ---
        recommendation = data.get("recommendation", "N/A")
        rec_color = colors.grey
        if recommendation == 'PROCEED': rec_color = colors.darkgreen
        elif recommendation == 'ENHANCED DUE DILIGENCE': rec_color = colors.orange
        elif recommendation == 'HIGH RISK': rec_color = colors.orangered
        elif recommendation == 'DO NOT PROCEED': rec_color = colors.darkred
        rec_style = ParagraphStyle(name='AMLRecommendation', parent=styles['h1'], fontSize=16, textColor=rec_color, alignment=TA_CENTER, spaceAfter=15)
        story.append(Paragraph(f"Recommendation: {recommendation}", rec_style))

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

        # Check if this is an OFAC report (different structure)
        if search_engine == "OFAC" or 'SANCTIONS ALERT' in answer_text or 'OFAC CLEAR' in answer_text:
            # Handle OFAC reports with specialized formatting
            format_ofac_for_pdf(answer_text, story, styles)
        else:
            # Handle AI-generated reports with section headings
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
                formatted_text = apply_inline_markdown(answer_text)
                story.append(Paragraph(formatted_text, body_style))

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

Based on the OFAC screening results above and your research, provide a comprehensive AML assessment with the following structure:

## Company Summary
- Basic company information and business activities
- Key executives and ownership structure
- Geographic presence and operations

## AML Risk Assessment

### OFAC Sanctions Analysis
- Incorporate the OFAC screening results above
- If any OFAC matches with 95%+ similarity scores were found, this is a CRITICAL RED FLAG
- If matches with 83-94% similarity were found, this requires enhanced verification
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

## Summary & Recommendation
Based on your analysis, provide a clear recommendation using ONE of these categories:
- **PROCEED**: Low risk, standard due diligence sufficient
- **ENHANCED DUE DILIGENCE**: Some concerns identified, additional review recommended
- **HIGH RISK**: Significant concerns, extensive documentation and approval required
- **DO NOT PROCEED**: Critical risk factors present, avoid business relationship

IMPORTANT: If OFAC matches with 95%+ similarity were found, the recommendation should be DO NOT PROCEED regardless of other factors.

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
        
        # Extract recommendation from response
        recommendation = None
        rec_patterns = [
            r"\*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*",
            r"Recommendation[:\s]*\*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*",
            r"- \*\*(PROCEED|ENHANCED DUE DILIGENCE|HIGH RISK|DO NOT PROCEED)\*\*"
        ]
        
        for pattern in rec_patterns:
            match = re.search(pattern, answer, re.IGNORECASE)
            if match:
                recommendation = match.group(1).upper()
                break
        
        # Override recommendation if high-risk sanctions found
        if high_risk_sanctions:
            recommendation = "DO NOT PROCEED"
        
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
            recommendation = None
            save_location_message = ""
            
            with st.spinner(f"🔍 Analyzing {name}..."):
                # Perform search based on selected engine
                if search_engine == "Comprehensive (AI + OFAC)":
                    result = search_with_comprehensive(name, selected_model)
                    if result["status"] == "success":
                        pdf_bytes = generate_pdf_bytes(name, result, "Comprehensive")
                        status = "success"
                        error_message = None
                        recommendation = result.get("recommendation")
                    else:
                        status = "failed"
                        error_message = result["error"]
                        recommendation = None
                        pdf_bytes = None
                
                elif search_engine == "AI Research Only":
                    result = search_with_perplexity(name, selected_model)
                    if result["status"] == "success":
                        pdf_bytes = generate_pdf_bytes(name, result, "AI Research")
                        status = "success"
                        error_message = None
                        recommendation = result.get("recommendation")
                    else:
                        status = "failed"
                        error_message = result["error"]
                        recommendation = None
                        pdf_bytes = None
                
                elif search_engine == "OFAC Sanctions Only":
                    ofac_result = search_with_ofac(name)
                    if "❌" in ofac_result:
                        status = "failed"
                        error_message = ofac_result
                        recommendation = None
                        pdf_bytes = None
                    else:
                        status = "success"
                        error_message = None
                        if "🚨" in ofac_result or "SANCTIONS ALERT" in ofac_result:
                            recommendation = "DO NOT PROCEED"
                        else:
                            recommendation = "PROCEED"
                        pdf_bytes = generate_pdf_bytes(name, {"answer": ofac_result, "recommendation": recommendation}, "OFAC")
                
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
                'recommendation': recommendation,
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
            recommendation = result.get('recommendation', 'N/A')
            save_msg = result.get('save_location_message', '')
            
            # Get color for recommendation display
            rec_color = get_recommendation_color(recommendation)
            
            if result['status'] == 'success' and result.get('pdf_bytes') is None and save_msg:
                st.success(f"✅ **{result['name']}** [{recommendation}] - {save_msg}")
            elif result['status'] == 'success' and result.get('pdf_bytes') is not None:
                st.info(f"📄 **{result['name']}** [{recommendation}] - {save_msg}")
                pdfs_for_zip.append(result)
            elif result['status'] == 'warning':
                st.warning(f"⚠️ **{result['name']}** [{recommendation}] - {save_msg}")
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
st.markdown("**AML Demo v1.20** | Powered by Perplexity AI & OFAC Database (83% match threshold)") 