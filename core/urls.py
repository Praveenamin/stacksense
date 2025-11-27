from django.urls import path
from django.contrib.auth import views as auth_views
from core.password_reset_views import CustomPasswordResetView
from . import views

urlpatterns = [
    path("", views.monitoring_dashboard, name="dashboard"),
    path("monitoring/", views.monitoring_dashboard, name="monitoring_dashboard"),
    path("dashboard/", views.monitoring_dashboard, name="dashboard_alt"),
    path("add-server/", views.add_server, name="add_server"),
    path("remove-server/<int:server_id>/", views.remove_server, name="remove_server"),
    path("api/live-metrics/", views.get_live_metrics, name="live_metrics"),
    path("api/alert-config/", views.alert_config, name="alert_config"),
    path("api/alert-config/test/", views.test_email_connection, name="test_email_connection"),
    path("api/server/<int:server_id>/thresholds/", views.update_thresholds, name="update_thresholds"),
    path("server/<int:server_id>/", views.server_details, name="server_details"),
    path("password-reset/", CustomPasswordResetView.as_view(template_name="admin/password_reset.html", email_template_name="admin/password_reset_email.html"), name="password_reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(template_name="admin/password_reset_done.html"), name="password_reset_done"),
    path("password-reset-confirm/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(template_name="admin/password_reset_confirm.html"), name="password_reset_confirm"),
    path("password-reset-complete/", auth_views.PasswordResetCompleteView.as_view(template_name="admin/password_reset_complete.html"), name="password_reset_complete"),
    path("admin-users/", views.admin_users, name="admin_users"),
    path("admin-users/create/", views.create_admin_user, name="create_admin_user"),
    path("admin-users/edit/<int:user_id>/", views.edit_admin_user, name="edit_admin_user"),
    path("admin-users/delete/<int:user_id>/", views.delete_admin_user, name="delete_admin_user"),
    path("api/admin-users/", views.admin_users_api, name="admin_users_api"),
    path("api/admin-users/create/", views.create_admin_user_api, name="create_admin_user_api"),
    path("api/admin-users/<int:user_id>/", views.admin_user_api, name="admin_user_api"),
]

