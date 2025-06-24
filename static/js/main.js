document.getElementById('searchButton').addEventListener('click', async () => {
    const namesInput = document.getElementById('names');
    const outputPathInput = document.getElementById('outputPath');
    const loading = document.getElementById('loading');
    const resultsDiv = document.getElementById('results');

    const names = namesInput.value;
    const outputPath = outputPathInput.value;

    if (!names || !outputPath) {
        alert('Please fill in all fields: Company Names and Output Folder Path');
        return;
    }

    loading.classList.add('active');
    resultsDiv.innerHTML = ''; // Clear previous results

    try {
        const response = await fetch('/search', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ names, output_path: outputPath }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(`HTTP error! status: ${response.status}, message: ${errorData.error || 'Unknown server error'}`);
        }

        const data = await response.json();

        if (data.error) {
            throw new Error(`Backend error: ${data.error}`);
        }

        // Display status for each processed name
        let overallStatusHtml = '<h3 class="text-lg font-semibold mb-3">Processing Results:</h3><ul class="space-y-2">';
        data.results.forEach(nameResult => {
            overallStatusHtml += `<li class="p-3 rounded ${nameResult.status === 'success' ? 'bg-green-100' : 'bg-red-100'}">`;
            overallStatusHtml += `<span class="font-medium">${nameResult.name}:</span> `;
            if (nameResult.status === 'success') {
                overallStatusHtml += `<span class="text-green-700">Success</span>`;
                // Optionally add link to PDF or mention path
                // overallStatusHtml += ` - PDF saved at: ${nameResult.pdf_path}`;
                 overallStatusHtml += ` - PDF saved in output folder.`;
            } else {
                overallStatusHtml += `<span class="text-red-700">Failed</span>`;
                if (nameResult.error_message) {
                    overallStatusHtml += ` - Error: ${nameResult.error_message}`;
                }
            }
            overallStatusHtml += `</li>`;
        });
        overallStatusHtml += '</ul>';
        resultsDiv.innerHTML = overallStatusHtml;

    } catch (error) {
        // Display general errors
        resultsDiv.innerHTML = `
            <div class="p-3 mb-2 rounded bg-red-100">
                <p class="text-sm text-red-700 font-semibold">An Error Occurred:</p>
                <p class="text-sm text-red-700">${error.message}</p>
            </div>
        `;
        console.error("Search Error:", error);
    } finally {
        loading.classList.remove('active');
    }
}); 