#!/usr/bin/python3
import time

import pyppeteer
import asyncio

loop = asyncio.get_event_loop()
async def goto_page_url(url):
    browser = await pyppeteer.launcher.connect(
        loop=loop,
        browserWSEndpoint='ws://localhost:3000'
    )
    page = await browser.newPage()
    await page.goto(url)
    content = await page.content()
    assert 'html' in content.lower(), "'html' should exist in the page fetch"
    await page.close()
    await browser.close()

if __name__ == '__main__':

    async def main():
        now = time.time()
        await asyncio.create_task(goto_page_url(url="https://google.com"))
        print (f"Done in {time.time()-now:.2f} seconds")
    loop.run_until_complete(main())
