import psutil
from aiohttp import web
from loguru import logger
import os
import asyncio
from functools import partial

async def handle_http_request(request, stats):
    # Create a task with timeout to prevent blocking during high load
    try:
        # Run the potentially CPU-intensive operations in a separate thread with timeouts
        loop = asyncio.get_event_loop()
        
        # Set timeout for system operations (3 seconds)
        TIMEOUT = 3
        
        # Use asyncio.wait_for to enforce timeouts on executor tasks
        try:
            # Run directly in executor without creating a task
            svmem = await asyncio.wait_for(
                loop.run_in_executor(None, psutil.virtual_memory),
                timeout=TIMEOUT
            )
            
            parent = psutil.Process(os.getpid())
            
            # Get child count directly in executor
            get_child_count = lambda: len(parent.children(recursive=False))
            child_count = await asyncio.wait_for(
                loop.run_in_executor(None, get_child_count),
                timeout=TIMEOUT
            )
            
            mem_use_percent = svmem.percent
            
        except asyncio.TimeoutError:
            # If system calls timeout, use fallback values
            logger.warning("System monitoring calls timed out in stats endpoint")
            child_count = stats.get('last_child_count', 0)
            mem_use_percent = stats.get('last_mem_percent', 0)
        else:
            # Save latest successful values for future fallback
            stats['last_child_count'] = child_count
            stats['last_mem_percent'] = mem_use_percent

        data = {
            'active_connections': stats['connection_count'],
            'child_count': child_count,
            'connection_count_total': stats['connection_count_total'],
            'dropped_threshold_reached': stats['dropped_threshold_reached'],
            'dropped_waited_too_long': stats['dropped_waited_too_long'],
            'mem_use_percent': mem_use_percent,
            'special_counter_len': len(stats['special_counter']),
        }

        return web.json_response(data, content_type='application/json')
    except asyncio.TimeoutError:
        logger.warning("Stats request timed out, returning partial data")
        # Return minimal stats data if timeout occurs
        return web.json_response({
            'active_connections': stats['connection_count'],
            'connection_count_total': stats['connection_count_total'],
            'error': 'request_timeout'
        }, content_type='application/json')
    except Exception as e:
        logger.error(f"Error in stats endpoint: {str(e)}")
        return web.json_response({
            'error': 'internal_error',
            'message': str(e)
        }, status=500, content_type='application/json')


async def start_http_server(host, port, stats):
    app = web.Application(client_max_size=1024)  # Limit request size
    # Create a route with a timeout middleware
    app.router.add_get('/stats', lambda req: handle_http_request(req, stats))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port, backlog=20)  # Limit connection backlog
    await site.start()
    logger.success(f"HTTP stats info server running at http://{host}:{port}/stats")

