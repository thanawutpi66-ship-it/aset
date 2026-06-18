"""
Service locator pattern for dependency injection
"""
from typing import Any, Dict, Type, TypeVar, Optional
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')

class ServiceLocator:
    """Service locator for dependency injection"""

    _instance = None
    _services: Dict[Type, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, service_type: Type[T], service_instance: T) -> None:
        """Register a service instance"""
        cls._services[service_type] = service_instance
        logger.debug(f"Registered service: {service_type.__name__}")

    @classmethod
    def get(cls, service_type: Type[T]) -> T:
        """Get a service instance"""
        if service_type not in cls._services:
            raise ValueError(f"Service {service_type.__name__} not registered")
        return cls._services[service_type]

    @classmethod
    def has(cls, service_type: Type[T]) -> bool:
        """Check if a service is registered"""
        return service_type in cls._services

    @classmethod
    def unregister(cls, service_type: Type[T]) -> None:
        """Unregister a service"""
        if service_type in cls._services:
            del cls._services[service_type]
            logger.debug(f"Unregistered service: {service_type.__name__}")

    @classmethod
    def clear(cls) -> None:
        """Clear all registered services"""
        cls._services.clear()
        logger.debug("Cleared all services")

    @classmethod
    def list_services(cls) -> Dict[str, str]:
        """List all registered services"""
        return {service_type.__name__: type(instance).__name__
                for service_type, instance in cls._services.items()}

class ServiceProvider:
    """Context manager for service registration"""

    def __init__(self):
        self._services_to_cleanup = []

    def register(self, service_type: Type[T], service_instance: T):
        """Register a service that will be cleaned up when exiting context"""
        ServiceLocator.register(service_type, service_instance)
        self._services_to_cleanup.append(service_type)
        return service_instance

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Clean up registered services
        for service_type in self._services_to_cleanup:
            ServiceLocator.unregister(service_type)
        self._services_to_cleanup.clear()