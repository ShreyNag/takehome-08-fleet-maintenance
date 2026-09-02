from django.urls import path

from . import views

urlpatterns = [
    path("vehicles/", views.VehicleListView.as_view(), name="vehicle-list"),
    path("vehicles/archived/", views.ArchivedVehicleListView.as_view(), name="vehicle-archived-list"),
    path("vehicles/create/", views.VehicleCreateView.as_view(), name="vehicle-create"),
    path("vehicles/<int:pk>/", views.VehicleDetailView.as_view(), name="vehicle-detail"),
    path("vehicles/<int:pk>/edit/", views.VehicleUpdateView.as_view(), name="vehicle-update"),
    path("vehicles/<int:pk>/archive/", views.VehicleArchiveView.as_view(), name="vehicle-archive"),
    path("vehicles/<int:pk>/restore/", views.VehicleRestoreView.as_view(), name="vehicle-restore"),
]
