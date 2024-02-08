#!/usr/bin/python3
import time

import pyppeteer
import asyncio
from loguru import logger

loop = asyncio.get_event_loop()


# @todo - disk_cache_dir could be Path type
async def goto_page_url(url, user_agent=None, req_headers: dict = None, proxy_username: str = None, proxy_password: str = None,
                        disk_cache_dir: str = None, extra_wait_ms: int = 0, execute_js: str = None, xpath_scrape_code: str = None,
                        instock_scrape_code: str = None, screenshot_quality: int = 60):
    xpath_data = None
    instock_data = None

    if req_headers is None:
        req_headers = {}

    #@todo logLevel
    #* ``defaultViewport`` (dict): Set a consistent viewport for each page.
      #Defaults to an 800x600 viewport. ``None`` disables default viewport.
      #* ``width`` (int): page width in pixels.
      #* ``height`` (int): page height in pixels.
    # @todo this could be optional or set somewhere
#    await page.setViewport({"width": 1024, "height": 768, "deviceScaleFactor": 1})

    #@todo control if it was really connected

    print ("Connecting")
    browser = await pyppeteer.launcher.connect(
        loop=loop,
        #browserWSEndpoint='ws://localhost:3000'
        browserWSEndpoint='ws://localhost:3000'
    )
    print("Connected")

    page = await browser.newPage()
    print("new page done")
    await page.setBypassCSP(True)
    if req_headers:
        await page.setExtraHTTPHeaders(req_headers)

    if user_agent:
        await page.setUserAgent(user_agent)

    # https://ourcodeworld.com/articles/read/1106/how-to-solve-puppeteer-timeouterror-navigation-timeout-of-30000-ms-exceeded
    page.setDefaultNavigationTimeout(0)
    print("setDefaultNavigationTimeout done")
    if proxy_username:
        # Setting Proxy-Authentication header is deprecated, and doing so can trigger header change errors from Puppeteer
        # https://github.com/puppeteer/puppeteer/issues/676 ?
        # https://help.brightdata.com/hc/en-us/articles/12632549957649-Proxy-Manager-How-to-Guides#h_01HAKWR4Q0AFS8RZTNYWRDFJC2
        # https://cri.dev/posts/2020-03-30-How-to-solve-Puppeteer-Chrome-Error-ERR_INVALID_ARGUMENT/
        await page.authenticate({
            'username': proxy_username,
            'password': proxy_password
        })


    #await page.setRequestInterception(True)

    if disk_cache_dir:
        logger.warning(f"Enabling local disk cache at {disk_cache_dir}, INCOMPLETE")
        #@todo
    print ("going to page")
    r = await page.goto(url, {"waitUntil": "load"})
    print("going to page done")
    await asyncio.sleep(1)
    print("going to page", extra_wait_ms)
    if extra_wait_ms:
        await asyncio.sleep(extra_wait_ms / 1000)

    if execute_js:
        logger.debug("Executing custom JS")
        await page.evaluate(execute_js)
        await asyncio.sleep(0.2)

    if xpath_scrape_code and instock_scrape_code:
        try:
            xpath_data = await page.evaluate(xpath_scrape_code)
            instock_data = await page.evaluate(instock_scrape_code)
        except Exception as e:
            logger.critical(str(e))
            pass

    # Protocol error (Page.captureScreenshot): "Cannot take screenshot with 0 width" can come from a proxy auth failure
    screenshot = None
    try:
        screenshot = await page.screenshot({"encoding": "binary", "fullPage": True, "quality": screenshot_quality, "type": 'jpeg'});
    except Exception as e:
        logger.error("Error fetching screenshot")
        # // May fail on very large pages with 'WARNING: tile memory limits exceeded, some content may not draw'
        #// @ todo after text extract, we can place some overlay text with red background to say 'croppped'
        logger.error('ERROR: content-fetcher page was maybe too large for a screenshot, reverting to viewport only screenshot')
        try:
            screenshot = await page.screenshot({"encoding": "binary", "quality": screenshot_quality, "type": 'jpeg'})
        except Exception as e:
            logger.error('ERROR: Failed to get viewport-only reduced screenshot :(')
            pass
    #await asyncio.sleep(1)
    html = await page.content()
    await asyncio.sleep(1)
    await page.close()
    # maybe reuseable
    #await browser.close()

    return {
        "data": {
            'content': html,
            'headers': r.headers,
            'instock_data': instock_data,
            'screenshot': screenshot,
            'status_code': r.status,
            'xpath_data': xpath_data
        },
        type: 'application/json',
    }



if __name__ == '__main__':
    async def main():
        now = time.time()
        res = await asyncio.create_task(goto_page_url(url="https://example.com"))
        print(f"Done in {time.time() - now:.2f} seconds")


    loop.run_until_complete(main())
