"""
Dependency injection container for the memory monitor application
"""

from dependency_injector import containers, providers

from .config import Config
from .managers.state_manager import StateManager
from .managers.process_manager import ProcessManager
from .managers.websocket_manager import WebSocketManager
from .services.monitoring_service import MonitoringService
from .services.logging_service import LoggingService


class Container(containers.DeclarativeContainer):
    """Main DI container for the application"""
    
    # Configuration - will be overridden with actual Config object
    config = providers.Object(None)
    
    # Services (Singletons)
    logging_service = providers.Singleton(
        LoggingService
    )
    
    # Managers (Singletons - shared across the app)
    state_manager = providers.Singleton(
        StateManager
    )
    
    process_manager = providers.Singleton(
        ProcessManager,
        config=config
    )
    
    websocket_manager = providers.Singleton(
        WebSocketManager,
        config=config
    )
    
    # Services
    monitoring_service = providers.Factory(
        MonitoringService,
        config=config,
        state_manager=state_manager,
        process_manager=process_manager,
        websocket_manager=websocket_manager,
        logging_service=logging_service
    )