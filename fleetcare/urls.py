"""
URL configuration for fleetcare project.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from accounts import views as accounts_views
from fleet.views import DashboardView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='dashboard'), name='home'),
    path('login/', accounts_views.FleetLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    # Goal 8: real aggregation logic, so it lives in fleet (which owns
    # Vehicle/ServiceRecord) rather than accounts -- the URL name/path stay
    # exactly as they were, so LOGIN_REDIRECT_URL and the 'home' redirect
    # above don't need to change.
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('', include('fleet.urls')),
]
