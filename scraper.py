import requests

class WebScraper:
    def __init__(self):
        pass

    def scrape_url(self, url: str) -> str:
        try:
            res = requests.get(url, timeout=15)
            return res.text[:2000] if res.status_code == 200 else f'Error: {res.status_code}'
        except Exception as e:
            return f'Error: {str(e)}'
