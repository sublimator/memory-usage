"""
WebSocket connection management
"""

import asyncio
from typing import Optional, Dict, Any, Callable, List
import logging

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import ServerInfo, Subscribe, Unsubscribe
from xrpl.models import Response

from ..config import Config


logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and subscriptions centrally"""
    
    def __init__(self, config: Config):
        self.config = config
        self.client: Optional[AsyncWebsocketClient] = None
        self._lock = asyncio.Lock()
        self._subscribed_streams: set = set()
        self._message_handlers: List[Callable] = []
        self._connection_retries = 0
        self._max_retries = 5
    
    async def connect(self) -> AsyncWebsocketClient:
        """Get or create WebSocket connection with retry logic"""
        async with self._lock:
            if self.client and self.client.is_open():
                return self.client
            
            # Reset retries on new connection attempt
            self._connection_retries = 0
            
            while self._connection_retries < self._max_retries:
                try:
                    logger.info(f"Attempting WebSocket connection to {self.config.websocket_url}")
                    self.client = AsyncWebsocketClient(self.config.websocket_url)
                    await self.client.open()
                    logger.info("WebSocket connection established")
                    
                    # Re-subscribe to any streams we were subscribed to
                    if self._subscribed_streams:
                        await self._resubscribe()
                    
                    # Start message handler
                    asyncio.create_task(self._message_loop())
                    
                    return self.client
                    
                except Exception as e:
                    self._connection_retries += 1
                    logger.warning(f"WebSocket connection attempt {self._connection_retries} failed: {e}")
                    
                    if self._connection_retries < self._max_retries:
                        await asyncio.sleep(5)  # Wait before retry
                    else:
                        raise ConnectionError(f"Failed to connect after {self._max_retries} attempts")
    
    async def disconnect(self):
        """Close WebSocket connection"""
        async with self._lock:
            if self.client and self.client.is_open():
                logger.info("Closing WebSocket connection")
                await self.client.close()
                self.client = None
                # Don't clear subscriptions - we might reconnect
    
    async def subscribe_to_streams(self, streams: List[str]) -> bool:
        """Subscribe to WebSocket streams"""
        if not self.client or not self.client.is_open():
            await self.connect()
        
        try:
            subscribe_request = Subscribe(streams=streams, api_version=self.config.api_version)
            response = await self.client.request(subscribe_request)
            
            if response.is_successful():
                self._subscribed_streams.update(streams)
                logger.info(f"Subscribed to streams: {streams}")
                return True
            else:
                logger.error(f"Failed to subscribe to streams: {response}")
                return False
                
        except Exception as e:
            logger.error(f"Error subscribing to streams: {e}")
            return False
    
    async def unsubscribe_from_streams(self, streams: List[str]) -> bool:
        """Unsubscribe from WebSocket streams"""
        if not self.client or not self.client.is_open():
            # Not connected, just update our state
            self._subscribed_streams.difference_update(streams)
            return True
        
        try:
            unsubscribe_request = Unsubscribe(streams=streams, api_version=self.config.api_version)
            response = await self.client.request(unsubscribe_request)
            
            if response.is_successful():
                self._subscribed_streams.difference_update(streams)
                logger.info(f"Unsubscribed from streams: {streams}")
                return True
            else:
                logger.error(f"Failed to unsubscribe from streams: {response}")
                return False
                
        except Exception as e:
            logger.error(f"Error unsubscribing from streams: {e}")
            return False
    
    async def get_server_info(self) -> Optional[Dict[str, Any]]:
        """Get server info"""
        if not self.client or not self.client.is_open():
            await self.connect()
        
        try:
            server_info_request = ServerInfo(api_version=self.config.api_version)
            response = await self.client.request(server_info_request)
            
            if response.is_successful():
                return response.result.get('info', {})
            else:
                logger.error(f"Server info request failed: {response}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting server info: {e}")
            return None
    
    def add_message_handler(self, handler: Callable):
        """Add a message handler"""
        self._message_handlers.append(handler)
    
    def remove_message_handler(self, handler: Callable):
        """Remove a message handler"""
        if handler in self._message_handlers:
            self._message_handlers.remove(handler)
    
    async def _message_loop(self):
        """Process incoming WebSocket messages"""
        try:
            async for message in self.client:
                if isinstance(message, dict):
                    # Notify all handlers
                    for handler in self._message_handlers:
                        try:
                            if asyncio.iscoroutinefunction(handler):
                                await handler(message)
                            else:
                                handler(message)
                        except Exception as e:
                            logger.error(f"Message handler error: {e}")
        except Exception as e:
            logger.error(f"Message loop error: {e}")
            # Try to reconnect
            asyncio.create_task(self.connect())
    
    async def _resubscribe(self):
        """Re-subscribe to streams after reconnection"""
        if self._subscribed_streams:
            logger.info(f"Re-subscribing to streams: {list(self._subscribed_streams)}")
            streams = list(self._subscribed_streams)
            self._subscribed_streams.clear()
            await self.subscribe_to_streams(streams)
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self.client is not None and self.client.is_open()