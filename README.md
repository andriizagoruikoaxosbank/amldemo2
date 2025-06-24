# AML Research Platform

Advanced Anti-Money Laundering Due Diligence System powered by Perplexity AI and OFAC sanctions database.

## Features
- AI-powered comprehensive due diligence research
- OFAC sanctions database screening
- Bulk processing of multiple subjects
- Word document report generation
- Authenticated access with password protection
- Real-time web search with authoritative sources

## Setup

### Local Development
1. Install Python 3.8 or higher
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your API keys:
   ```
   PERPLEXITY_API_KEY=your_perplexity_api_key_here
   OPENAI_API_KEY=your_openai_api_key_here
   ```
4. Run the application:
   ```bash
   streamlit run streamlit_app.py
   ```

### Streamlit Cloud Deployment
1. Fork this repository to your GitHub account
2. Connect your GitHub repo to Streamlit Cloud
3. In Streamlit Cloud, go to your app settings and add secrets:
   ```toml
   PERPLEXITY_API_KEY = "your_perplexity_api_key_here"
   OPENAI_API_KEY = "your_openai_api_key_here"
   ```
4. Deploy the app

## Usage
1. Access the application (locally or deployed URL)
2. Enter the password: `AML2024secure!`
3. Select your search method:
   - **Comprehensive (AI + OFAC)**: Full research with sanctions screening
   - **AI Research Only**: Perplexity AI research without OFAC
   - **OFAC Sanctions Only**: Sanctions database screening only
4. Choose AI model (Sonar Pro or Sonar Deep Research)
5. Enter subject names (one per line)
6. Click "Generate AML Reports"
7. Download the Word document with results

## Security Notes
- Never commit API keys to version control
- Use environment variables or Streamlit secrets for sensitive data
- Change the default password in production
- Review and sanitize any logs containing sensitive information 