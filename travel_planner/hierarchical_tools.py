"""Backward-compatible exports of the original hierarchical mock tool schemas."""
from domains.travel_legacy.tools import (
    book_car, book_flight, book_hotel, check_room, check_seat, check_wait_time,
    get_emergency_contacts, get_hourly_forecast, get_menu, get_opening_hours,
    get_weather, search_attractions, search_cars, search_flights, search_hotels,
    search_restaurants, translate,
)

__all__ = [
    "book_car", "book_flight", "book_hotel", "check_room", "check_seat", "check_wait_time",
    "get_emergency_contacts", "get_hourly_forecast", "get_menu", "get_opening_hours",
    "get_weather", "search_attractions", "search_cars", "search_flights", "search_hotels",
    "search_restaurants", "translate",
]
