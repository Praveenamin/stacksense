from django.contrib import admin
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

# Override admin index to redirect to monitoring
original_index = admin.site.index

@login_required
def custom_admin_index(request, extra_context=None):
    # Redirect authenticated users to monitoring
    return redirect("/monitoring/")

# Replace admin index
admin.site.index = custom_admin_index
