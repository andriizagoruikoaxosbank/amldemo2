# Bulk Negative News Research Tool

This tool allows analysts to perform bulk negative news research on multiple parties by automatically searching Google and saving results as PDFs.

## Features
- Bulk search for negative news across multiple parties
- Automated Google search with predefined negative news keywords
- PDF export of search results
- Custom output folder selection
- Modern web interface

## Setup
1. Install Python 3.8 or higher
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your API keys:
   ```
   GEMINI_API_KEY=your_gemini_key
   TAVILY_API_KEY=your_tavily_key
   ```
4. Run the application:
   ```bash
   python app.py
   ```

## Usage
1. Open your browser and navigate to `http://localhost:5000`
2. Paste the list of names to search (one per line)
3. Select the output folder path
4. Click "Start Search" to begin the process

## Note
Make sure you have sufficient permissions to write to the selected output folder path. 