from django.urls import path

from . import views

urlpatterns = [
    path("vehicles/", views.VehicleListView.as_view(), name="vehicle-list"),
    path("vehicles/<int:pk>/", views.VehicleDetailView.as_view(), name="vehicle-detail"),
]
