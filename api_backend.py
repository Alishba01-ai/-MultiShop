# app_ecommerce_apis.py
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# API Configuration
WALMART_API_KEY = os.getenv('WALMART_API_KEY')
EBAY_TOKEN = os.getenv('EBAY_TOKEN')
BESTBUY_API_KEY = os.getenv('BESTBUY_API_KEY')
ETSY_API_KEY = os.getenv('ETSY_API_KEY')

class WalmartAPI:
    BASE_URL = "https://api.walmartlabs.com/v1"
    
    def search(self, query, max_results=10):
        url = f"{self.BASE_URL}/search"
        params = {
            'apiKey': WALMART_API_KEY,
            'query': query,
            'numItems': max_results
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            products = []
            for item in data.get('items', []):
                products.append({
                    'title': item.get('name'),
                    'price': f"${item.get('salePrice', 0)}",
                    'image': item.get('thumbnailImage'),
                    'link': item.get('productUrl'),
                    'source': 'Walmart'
                })
            
            return products
        except Exception as e:
            print(f"Walmart API error: {e}")
            return []

class eBayAPI:
    BASE_URL = "https://api.ebay.com/buy/browse/v1"
    
    def search(self, query, max_results=10):
        url = f"{self.BASE_URL}/item_summary/search"
        headers = {
            'Authorization': f'Bearer {EBAY_TOKEN}',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
        }
        params = {'q': query, 'limit': max_results}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            data = response.json()
            
            products = []
            for item in data.get('itemSummaries', []):
                products.append({
                    'title': item.get('title'),
                    'price': item.get('price', {}).get('value'),
                    'image': item.get('image', {}).get('imageUrl'),
                    'link': item.get('itemWebUrl'),
                    'source': 'eBay'
                })
            
            return products
        except Exception as e:
            print(f"eBay API error: {e}")
            return []

class BestBuyAPI:
    BASE_URL = "https://api.bestbuy.com/v1"
    
    def search(self, query, max_results=10):
        url = f"{self.BASE_URL}/products((search={query}))"
        params = {
            'apiKey': BESTBUY_API_KEY,
            'format': 'json',
            'pageSize': max_results,
            'show': 'name,salePrice,image,url'
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            products = []
            for item in data.get('products', []):
                products.append({
                    'title': item.get('name'),
                    'price': f"${item.get('salePrice', 0)}",
                    'image': item.get('image'),
                    'link': item.get('url'),
                    'source': 'Best Buy'
                })
            
            return products
        except Exception as e:
            print(f"Best Buy API error: {e}")
            return []

class EtsyAPI:
    BASE_URL = "https://openapi.etsy.com/v3/application"
    
    def search(self, query, max_results=10):
        url = f"{self.BASE_URL}/listings/active"
        headers = {'x-api-key': ETSY_API_KEY}
        params = {
            'keywords': query,
            'limit': max_results
        }
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            data = response.json()
            
            products = []
            for item in data.get('results', []):
                products.append({
                    'title': item.get('title'),
                    'price': f"${item.get('price', {}).get('amount', 0) / 100}",
                    'image': item.get('images', [{}])[0].get('url_570xN'),
                    'link': item.get('url'),
                    'source': 'Etsy'
                })
            
            return products
        except Exception as e:
            print(f"Etsy API error: {e}")
            return []

# Initialize API clients
walmart_api = WalmartAPI()
ebay_api = eBayAPI()
bestbuy_api = BestBuyAPI()
etsy_api = EtsyAPI()

API_CLIENTS = {
    'walmart': walmart_api,
    'ebay': ebay_api,
    'bestbuy': bestbuy_api,
    'etsy': etsy_api
}

@app.route('/api/search', methods=['POST'])
def search_products():
    data = request.get_json()
    query = data.get('query', '').strip()
    platforms = data.get('platforms', [])
    max_results = min(int(data.get('max_results', 10)), 50)
    
    if not query or not platforms:
        return jsonify({'error': 'Query and platforms required'}), 400
    
    all_products = []
    
    for platform in platforms:
        if platform in API_CLIENTS:
            products = API_CLIENTS[platform].search(query, max_results)
            all_products.extend(products)
    
    return jsonify({
        'success': True,
        'query': query,
        'total': len(all_products),
        'products': all_products
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
