'use strict';

window.addEventListener('load', function () {
  console.log('Page loaded, initializing Firebase Auth');
  console.log('URL:', window.location.href);
  console.log('Current cookies:', document.cookie);
  
  // Helper function to set cookie with proper attributes
  function setCookie(name, value) {
    var cookieString = name + "=" + value + ";path=/;SameSite=Lax;max-age=3600";
    if (window.location.protocol === 'https:') {
      cookieString += ";Secure";
    }
    document.cookie = cookieString;
    console.log('Cookie set:', name, '=', value.substring(0, 20) + '...');
  }
  
  // Set persistence to LOCAL to ensure auth state survives page reloads
  firebase.auth().setPersistence(firebase.auth.Auth.Persistence.LOCAL)
    .then(function() {
      console.log('Auth persistence set to LOCAL');
      initializeAuth();
    })
    .catch(function(error) {
      console.error('Error setting persistence:', error);
      initializeAuth();
    });
  
  function initializeAuth() {
    console.log('Initializing auth, current user:', firebase.auth().currentUser);
    
    // FirebaseUI config - use POPUP mode to avoid redirect issues
    var uiConfig = {
      signInFlow: 'popup',  // Use popup instead of redirect
      signInOptions: [
        firebase.auth.GoogleAuthProvider.PROVIDER_ID,
        firebase.auth.EmailAuthProvider.PROVIDER_ID,
      ],
      tosUrl: '<your-tos-url>',
      callbacks: {
        signInSuccessWithAuthResult: function(authResult, redirectUrl) {
          console.log('signInSuccessWithAuthResult called', authResult.user.email);
          return authResult.user.getIdToken().then(function(token) {
            console.log('Got token in callback, length:', token.length);
            setCookie("token", token);
            document.getElementById('login-info').hidden = false;
            // Don't redirect - return false to stay on same page
            return false;
          });
        },
        uiShown: function() {
          console.log('FirebaseUI widget shown');
        }
      }
    };

    // Initialize the FirebaseUI Widget using Firebase.
    var ui = firebaseui.auth.AuthUI.getInstance() || new firebaseui.auth.AuthUI(firebase.auth());
    console.log('FirebaseUI instance created');

    firebase.auth().onAuthStateChanged(function (user) {
      console.log('onAuthStateChanged fired, user:', user ? user.email : 'null');
      if (user) {
        // User is signed in
        document.getElementById('login-info').hidden = false;
        console.log(`Signed in as ${user.displayName} (${user.email})`);
        user.getIdToken().then(function (token) {
          console.log('Setting token cookie in onAuthStateChanged, length:', token.length);
          setCookie("token", token);
          console.log('Current cookies:', document.cookie);
        });
      } else {
        // User is signed out.
        console.log('User is null, showing FirebaseUI');
        // Check if FirebaseUI is already rendering (to avoid re-initialization)
        var container = document.getElementById('firebaseui-auth-container');
        if (container && container.children.length === 0) {
          console.log('Starting FirebaseUI widget');
          ui.start('#firebaseui-auth-container', uiConfig);
        }
        // Update the login state indicators.
        document.getElementById('login-info').hidden = true;
        // Clear the token cookie.
        setCookie("token", "");
        console.log('Token cookie cleared');
      }
    }, function (error) {
      console.error('onAuthStateChanged error:', error);
      alert('Unable to log in: ' + error)
    });
  }
});
