CLIA Downloader
==================

I would like you to download a CSV file from the following website. 

https://qcor.cms.gov/advanced_find_provider.jsp?which=4&backReport=active_CLIA.jsp

This website is the publicly available Clea data, and to download the CSV, all you have to do is click the download button.

I would prefer to have a Python script that posts the necessary values to the CSV downloader. You will have to parse the HTML and look at the download function that is being used and called by JavaScript when the CSV download button is pressed. You should have no filtering arguments, and you should save the CSV into a cache directory that is defined in the .env file. There should be a .env.example file which shows how that file should be set up. The Python script should be a command-line interface. 