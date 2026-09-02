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
    path(
        "vehicles/<int:vehicle_pk>/service-records/create/",
        views.ServiceRecordCreateView.as_view(),
        name="service-record-create",
    ),
    path("service-records/<int:pk>/", views.ServiceRecordDetailView.as_view(), name="service-record-detail"),
    path("service-records/<int:pk>/edit/", views.ServiceRecordUpdateView.as_view(), name="service-record-update"),
    path("service-records/<int:pk>/book/", views.ServiceRecordBookView.as_view(), name="service-record-book"),
    path("service-records/<int:pk>/start/", views.ServiceRecordStartView.as_view(), name="service-record-start"),
    path(
        "service-records/<int:pk>/complete/",
        views.ServiceRecordCompleteView.as_view(),
        name="service-record-complete",
    ),
    path(
        "service-records/<int:pk>/notes/",
        views.ServiceRecordAddNoteView.as_view(),
        name="service-record-add-note",
    ),
]
