from flask import Flask, request, jsonify
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import concurrent.futures
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend communication

class ProductScraper:
    """Base class for web scraping with Selenium"""
    
    def __init__(self):
        self.driver = None
    
    def setup_driver(self):
        """Initialize Chrome driver with headless options"""
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.set_page_load_timeout(30)
    
    def close_driver(self):
        """Close the browser driver"""
        if self.driver:
            self.driver.quit()
    
    def safe_find_element(self, by, value, default=""):
        """Safely find element and return text or default value"""
        try:
            element = self.driver.find_element(by, value)
            return element.text.strip() if element.text else default
        except NoSuchElementException:
            return default
    
    def safe_find_attribute(self, by, value, attribute, default=""):
        """Safely find element and return attribute or default value"""
        try:
            element = self.driver.find_element(by, value)
            return element.get_attribute(attribute) or default
        except NoSuchElementException:
            return default

class DarazScraper(ProductScraper):
    """Scraper for Daraz.pk"""
    
    def scrape(self, query, max_results=10):
        products = []
        try:
            self.setup_driver()
            search_url = f"https://www.daraz.pk/catalog/?q={query}"
            logger.info(f"Scraping Daraz for: {query}")
            
            self.driver.get(search_url)
            time.sleep(3)  # Wait for dynamic content
            
            # Wait for product cards to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-qa-locator='product-item']"))
            )
            
            product_cards = self.driver.find_elements(By.CSS_SELECTOR, "[data-qa-locator='product-item']")
            
            for card in product_cards[:max_results]:
                try:
                    # Get title from the link with title attribute
                    title_elem = card.find_element(By.CSS_SELECTOR, ".RfADt a")
                    title = title_elem.get_attribute("title") or title_elem.text
                    
                    # Get price from the span with class ooOxS
                    price = card.find_element(By.CSS_SELECTOR, ".ooOxS").text
                    
                    # Get image from the img tag
                    image = card.find_element(By.CSS_SELECTOR, "img[type='product']").get_attribute("src")
                    
                    # Get link from the anchor tag
                    link_elem = card.find_element(By.CSS_SELECTOR, ".RfADt a")
                    link = link_elem.get_attribute("href")
                    
                    # Ensure link is absolute URL
                    if link.startswith("//"):
                        link = "https:" + link
                    elif link.startswith("/"):
                        link = "https://www.daraz.pk" + link
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "image": image,
                        "link": link,
                        "source": "Daraz"
                    })
                except Exception as e:
                    logger.warning(f"Error parsing Daraz product: {e}")
                    continue
                    
        except TimeoutException:
            logger.error("Daraz: Timeout waiting for products to load")
        except Exception as e:
            logger.error(f"Daraz scraping error: {e}")
        finally:
            self.close_driver()
        
        return products
class TemuScraper(ProductScraper):
    """Scraper for Temu"""
    
    def scrape(self, query, max_results=10):
        products = []
        try:
            self.setup_driver()
            search_url = f"https://www.temu.com/search_result.html?search_key={query}"
            logger.info(f"Scraping Temu for: {query}")
            
            self.driver.get(search_url)
            time.sleep(4)
            
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='goods-item']"))
            )
            
            product_cards = self.driver.find_elements(By.CSS_SELECTOR, "[class*='goods-item']")
            
            for card in product_cards[:max_results]:
                try:
                    title_elem = card.find_element(By.CSS_SELECTOR, "[class*='title']")
                    price_elem = card.find_element(By.CSS_SELECTOR, "[class*='price']")
                    image_elem = card.find_element(By.TAG_NAME, "img")
                    link_elem = card.find_element(By.TAG_NAME, "a")
                    
                    products.append({
                        "title": title_elem.text,
                        "price": price_elem.text,
                        "image": image_elem.get_attribute("src"),
                        "link": link_elem.get_attribute("href"),
                        "source": "Temu"
                    })
                except Exception as e:
                    logger.warning(f"Error parsing Temu product: {e}")
                    continue
                    
        except TimeoutException:
            logger.error("Temu: Timeout waiting for products to load")
        except Exception as e:
            logger.error(f"Temu scraping error: {e}")
        finally:
            self.close_driver()
        
        return products

class SheinScraper(ProductScraper):
    """Scraper for Shein"""
    
    def scrape(self, query, max_results=10):
        products = []
        try:
            self.setup_driver()
            search_url = f"https://www.shein.com/search.html?q={query}"
            logger.info(f"Scraping Shein for: {query}")
            
            self.driver.get(search_url)
            time.sleep(4)
            
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='product-card']"))
            )
            
            product_cards = self.driver.find_elements(By.CSS_SELECTOR, "[class*='product-card']")
            
            for card in product_cards[:max_results]:
                try:
                    title = card.find_element(By.CSS_SELECTOR, "[class*='product-title']").text
                    price = card.find_element(By.CSS_SELECTOR, "[class*='price']").text
                    image = card.find_element(By.TAG_NAME, "img").get_attribute("src")
                    link = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "image": image,
                        "link": link,
                        "source": "Shein"
                    })
                except Exception as e:
                    logger.warning(f"Error parsing Shein product: {e}")
                    continue
                    
        except TimeoutException:
            logger.error("Shein: Timeout waiting for products to load")
        except Exception as e:
            logger.error(f"Shein scraping error: {e}")
        finally:
            self.close_driver()
        
        return products

class AlibabaScraper(ProductScraper):
    """Scraper for Alibaba"""
    
    def scrape(self, query, max_results=10):
        products = []
        try:
            self.setup_driver()
            search_url = f"https://www.alibaba.com/trade/search?SearchText={query}"
            logger.info(f"Scraping Alibaba for: {query}")
            
            self.driver.get(search_url)
            time.sleep(4)
            
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='product-item']"))
            )
            
            product_cards = self.driver.find_elements(By.CSS_SELECTOR, "[class*='product-item']")
            
            for card in product_cards[:max_results]:
                try:
                    title = card.find_element(By.CSS_SELECTOR, "[class*='title']").text
                    price = card.find_element(By.CSS_SELECTOR, "[class*='price']").text
                    image = card.find_element(By.TAG_NAME, "img").get_attribute("src")
                    link = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "image": image,
                        "link": link,
                        "source": "Alibaba"
                    })
                except Exception as e:
                    logger.warning(f"Error parsing Alibaba product: {e}")
                    continue
                    
        except TimeoutException:
            logger.error("Alibaba: Timeout waiting for products to load")
        except Exception as e:
            logger.error(f"Alibaba scraping error: {e}")
        finally:
            self.close_driver()
        
        return products

class AliExpressScraper(ProductScraper):
    """Scraper for AliExpress"""
    
    def scrape(self, query, max_results=10):
        products = []
        try:
            self.setup_driver()
            search_url = f"https://www.aliexpress.com/wholesale?SearchText={query}"
            logger.info(f"Scraping AliExpress for: {query}")
            
            self.driver.get(search_url)
            time.sleep(4)
            
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='product-item']"))
            )
            
            product_cards = self.driver.find_elements(By.CSS_SELECTOR, "[class*='product-item']")
            
            for card in product_cards[:max_results]:
                try:
                    title = card.find_element(By.CSS_SELECTOR, "[class*='title']").text
                    price = card.find_element(By.CSS_SELECTOR, "[class*='price']").text
                    image = card.find_element(By.TAG_NAME, "img").get_attribute("src")
                    link = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "image": image,
                        "link": link,
                        "source": "AliExpress"
                    })
                except Exception as e:
                    logger.warning(f"Error parsing AliExpress product: {e}")
                    continue
                    
        except TimeoutException:
            logger.error("AliExpress: Timeout waiting for products to load")
        except Exception as e:
            logger.error(f"AliExpress scraping error: {e}")
        finally:
            self.close_driver()
        
        return products

# Scraper mapping
SCRAPERS = {
    'daraz': DarazScraper,
    'temu': TemuScraper,
    'shein': SheinScraper,
    'alibaba': AlibabaScraper,
    'aliexpress': AliExpressScraper
}

@app.route('/api/search', methods=['POST'])
def search_products():
    """
    Main API endpoint for product search
    Expects JSON: {"query": "product name", "platforms": ["daraz", "temu", ...]}
    """
    try:
        data = request.get_json()
        query = data.get('query', '').strip()
        platforms = data.get('platforms', [])
        
        if not query:
            return jsonify({"error": "Query parameter is required"}), 400
        
        if not platforms:
            return jsonify({"error": "At least one platform must be selected"}), 400
        
        # Validate platforms
        invalid_platforms = [p for p in platforms if p not in SCRAPERS]
        if invalid_platforms:
            return jsonify({"error": f"Invalid platforms: {invalid_platforms}"}), 400
        
        logger.info(f"Search request: query='{query}', platforms={platforms}")
        
        # Scrape from multiple platforms concurrently
        all_products = []
        
        def scrape_platform(platform_name):
            try:
                scraper_class = SCRAPERS[platform_name]
                scraper = scraper_class()
                return scraper.scrape(query, max_results=10)
            except Exception as e:
                logger.error(f"Error scraping {platform_name}: {e}")
                return []
        
        # Use ThreadPoolExecutor for concurrent scraping
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_platform = {
                executor.submit(scrape_platform, platform): platform 
                for platform in platforms
            }
            
            for future in concurrent.futures.as_completed(future_to_platform):
                platform = future_to_platform[future]
                try:
                    products = future.result()
                    all_products.extend(products)
                    logger.info(f"Retrieved {len(products)} products from {platform}")
                except Exception as e:
                    logger.error(f"Error retrieving results from {platform}: {e}")
        
        return jsonify({
            "success": True,
            "query": query,
            "total_results": len(all_products),
            "products": all_products
        })
        
    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "message": "Backend is running"})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)