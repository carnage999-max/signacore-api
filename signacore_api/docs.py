from django.views.generic import TemplateView


class SignacoreApiDocsView(TemplateView):
    template_name = "signacore_api/api_docs.html"

